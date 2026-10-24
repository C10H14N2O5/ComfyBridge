"""Exclusive, durable idle cache operations. A timer never owns a business request."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from .comfy_client import ComfyError, SubmissionRejected, SubmissionUncertain
from .journal import CACHE_ELIGIBLE, TERMINAL
from .logging_utils import event


class CacheScheduler:
    def __init__(self, service, *, clock=None, wall_clock=None):
        self.service = service
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time
        self.activity_generation = 0
        self.idle_since = None
        self.preparing = False
        self.submitted = False
        self.active = set()
        self.deadlines = {}
        self.next_check = 0.0

    @property
    def journal(self):
        return self.service.journal

    @property
    def busy(self):
        return self.preparing or bool(self.active)

    @property
    def preflight(self):
        return self.preparing and not self.submitted

    def metadata(self, loaded):
        idle = getattr(loaded, "idle_release", None)
        if idle is None:
            return None
        preset = loaded.preset
        model = next((item for item in self.service.config.models if item.id == preset.model), None)
        if model is None or model.cache_policy != "idle_release":
            return None
        identity = {"model": model.model_dump(mode="json", exclude_none=True),
                    "preset": preset.model_dump(mode="json", exclude_none=True)}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        return {"model": preset.model, "backend_url": self.service.config.comfyui.base_url.rstrip("/"),
                "preset_id": preset.id, "template_version": preset.sha256, "config_version": digest,
                "voice": preset.voice, "method": idle.method, "seconds": idle.seconds}

    def loaded_for(self, row):
        loaded = self.service.presets.get((row["model"], row["voice"]))
        metadata = self.metadata(loaded) if loaded is not None else None
        if metadata is None or any(metadata[key] != row[key] for key in metadata):
            return None
        return loaded

    def recover(self):
        self.journal.recover_free()
        for row in self.journal.unresolved():
            if row["purpose"] == "cache_cleanup":
                self.active.add(row["task_id"])
                remaining = max(0.0, (row["deadline"] or 0.0) - self.wall_clock())
                self.deadlines[row["task_id"]] = self.clock() + remaining
        for row in self.journal.cache_cycles():
            if self.loaded_for(row) is None:
                if row['state'] != 'stale':
                    self.journal.set_cache_state(row["model"], row["use_generation"], "stale", "cache_config_stale")
            elif row["state"] in CACHE_ELIGIBLE:
                # A process restart is never credited with an elapsed idle period.
                self.journal.set_cache_state(row["model"], row["use_generation"], "pending_idle")
            elif row["state"] == "releasing" and row["operation_id"] not in self.active:
                # Fail closed if an interrupted/older implementation left an orphan intent.
                self.journal.set_cache_state(row["model"], row["use_generation"], "uncertain", "cache_release_uncertain")
        self.idle_since = None
        self.prune_retired()

    def prune_retired(self):
        self.journal.prune_retired_cache(model.id for model in self.service.config.models)

    def activity(self):
        self.activity_generation += 1
        self.idle_since = None
        self.next_check = 0.0
        for row in self.journal.cache_cycles():
            if row["state"] in CACHE_ELIGIBLE:
                self.journal.set_cache_state(row["model"], row["use_generation"], "pending_idle")

    def business_idle(self):
        service = self.service
        return not service._live and not service._queue and not service._remote and service._slots == 0 and service._delivery == 0

    def tick(self):
        service = self.service
        if service._draining or service._closed or self.busy:
            return
        if not self.business_idle():
            self.idle_since = None
            return
        rows = []
        for row in self.journal.cache_cycles():
            if self.loaded_for(row) is None:
                if row["state"] != "stale":
                    self.journal.set_cache_state(row["model"], row["use_generation"], "stale", "cache_config_stale")
            elif row["state"] in CACHE_ELIGIBLE:
                rows.append(row)
        if not rows:
            return
        now = self.clock()
        if self.idle_since is None:
            self.idle_since = now
        for row in rows:
            if row["state"] == "pending_idle":
                self.journal.set_cache_state(row["model"], row["use_generation"], "countdown")
        if now < self.next_check:
            return
        due = [row for row in rows if now - self.idle_since >= row["seconds"]]
        if not due:
            return
        selected = min(due, key=lambda row: (row["seconds"], row["model"]))
        # Reserve one slot for bounded admissions, and globally gate all business dispatch.
        self.preparing = True
        self.submitted = False
        service._slots += 1
        service._spawn(self._prepare(selected, self.activity_generation))

    def _still_idle(self, generation):
        service = self.service
        return (generation == self.activity_generation and not service._draining and not service._closed
                and not service._live and not service._queue and not service._remote
                and service._slots == 1 and service._delivery == 0)

    def _postpone(self, row, code, *, offline=False):
        self.journal.set_cache_state(row["model"], row["use_generation"], "waiting_remote", code)
        self.next_check = self.clock() + (self.service.config.limits.probe_interval if offline else self.service.config.limits.poll_interval)

    async def _prepare(self, selected, generation):
        service = self.service
        handed_to_follower = False
        operation_id = None
        try:
            loaded = self.loaded_for(selected)
            if loaded is None:
                self.journal.set_cache_state(selected["model"], selected["use_generation"], "stale", "cache_config_stale")
                return
            if not service._online or (selected["method"] == "workflow" and service._ready.get((selected["model"], selected["voice"]))):
                self._postpone(selected, "cache_remote_unavailable", offline=True)
                return
            client = service._client(selected["backend_url"])
            try:
                async with asyncio.timeout(service.config.limits.query_timeout):
                    queue = await client.queue()
                if (not isinstance(queue, dict) or not isinstance(queue.get("queue_running"), list)
                        or not isinstance(queue.get("queue_pending"), list)):
                    raise ComfyError("remote_protocol_error")
            except (ComfyError, asyncio.TimeoutError):
                if self._still_idle(generation):
                    self._postpone(selected, "cache_remote_unavailable", offline=True)
                return
            # A business admission during GET/preparation invalidates this attempt, even
            # if that request has already disconnected by the time the GET returns.
            if not self._still_idle(generation):
                return
            if queue["queue_running"] or queue["queue_pending"]:
                self._postpone(selected, "cache_remote_busy")
                return
            if selected["method"] == "free":
                members = [row for row in self.journal.cache_cycles()
                           if row["state"] in CACHE_ELIGIBLE and row["method"] == "free"
                           and row["backend_url"] == selected["backend_url"] and self.loaded_for(row) is not None]
                if not members or not self._still_idle(generation):
                    return
                operation_id = str(uuid.uuid4())
                self.journal.free_intent(operation_id, selected["backend_url"], members)
                self.submitted = True
                event("cache_release_intent", model=selected["model"], count=len(members))
                # No await exists between the generation check, durable intent and POST.
                try:
                    async with asyncio.timeout(service.config.limits.submit_timeout):
                        await client.free()
                    self.journal.finish_free(operation_id, "requested")
                    event("cache_release_requested", count=len(members))
                except (ComfyError, asyncio.TimeoutError) as error:
                    code = getattr(error, "code", "cache_free_uncertain")
                    ambiguous = code != "cache_free_http_error"
                    self.journal.finish_free(operation_id, "uncertain" if ambiguous else "failed",
                                             "cache_free_uncertain" if ambiguous else "cache_free_failed")
                    event("cache_release_uncertain" if ambiguous else "cache_release_failed", code="cache_free_uncertain" if ambiguous else "cache_free_failed")
                except asyncio.CancelledError:
                    # Forced exit after intent never retries /free on restart.
                    self.journal.finish_free(operation_id, "uncertain", "cache_free_uncertain")
                    raise
                except Exception:
                    self.journal.finish_free(operation_id, "uncertain", "cache_free_uncertain")
                return
            try:
                seed = self.journal.allocate_cleanup_seed()
            except ValueError:
                self.journal.set_cache_state(selected["model"], selected["use_generation"], "failed", "cache_seed_exhausted")
                return
            try:
                graph = loaded.build_cleanup_prompt(loaded.idle_release.text, seed)
            except Exception:
                self.journal.set_cache_state(selected["model"], selected["use_generation"], "failed", "cache_build_failed")
                return
            if not self._still_idle(generation):
                return
            operation_id = str(uuid.uuid4())
            deadline = self.clock() + service.config.limits.request_timeout
            row = self.journal.intent(task_id=operation_id, request_id=str(uuid.uuid4()),
                backend_url=selected["backend_url"], template_version=selected["template_version"],
                config_version=selected["config_version"], purpose="cache_cleanup", model=selected["model"],
                voice=selected["voice"], preset_id=selected["preset_id"], cache_generation=selected["use_generation"],
                deadline=self.wall_clock() + service.config.limits.request_timeout)
            self.submitted = True
            self.active.add(operation_id)
            self.deadlines[operation_id] = deadline
            service._remote[operation_id] = None
            handed_to_follower = True
            self.preparing = False
            event("cache_release_intent", model=selected["model"], prompt_id=operation_id)
            try:
                async with asyncio.timeout(min(service.config.limits.submit_timeout, service.config.limits.request_timeout)):
                    await client.submit(graph, operation_id, row["instance_id"], row["request_id"])
                self.journal.update(operation_id, state="pending", admitted=1)
            except SubmissionRejected:
                self.journal.update(operation_id, state="error", error_code="submission_rejected")
            except (ComfyError, asyncio.TimeoutError):
                self.journal.update(operation_id, state="uncertain", error_code="submission_uncertain")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.journal.update(operation_id, state="uncertain", error_code="internal_error")
            finally:
                del graph
            await service._follow(operation_id, None)
        finally:
            if not handed_to_follower:
                self.preparing = False
                self.submitted = False
                service._release(None)
            service._wake.set()

    def expire(self, row):
        if row["purpose"] != "cache_cleanup":
            return row
        deadline = self.deadlines.get(row["task_id"], self.clock())
        if not row["expired"] and self.clock() >= deadline:
            return self.journal.update(row["task_id"], expired=1, client_state="abandoned")
        return row

    def finished(self, task_id):
        if task_id not in self.active:
            return
        row = self.journal.get(task_id)
        if row["state"] not in TERMINAL:
            return
        self.active.discard(task_id)
        if not self.active:
            self.submitted = False
        self.deadlines.pop(task_id, None)
        state = "completed" if row["state"] == "success" else ("uncertain" if row["state"] == "resolved" else "failed")
        event("cache_release_" + state, model=row["model"], prompt_id=task_id, state=state)

    def status(self):
        result = []
        now = self.clock()
        for row in self.journal.cache_cycles():
            state = row["state"]
            remaining = None
            if state in CACHE_ELIGIBLE and self.idle_since is not None:
                remaining = round(max(0.0, row["seconds"] - (now - self.idle_since)), 3)
            if state == "pending_idle":
                remaining = None
            result.append({"model": row["model"], "voice": row["voice"], "method": row["method"],
                           "seconds": row["seconds"], "remaining_seconds": remaining,
                           "state": state, "error_code": row["error_code"]})
        return result
