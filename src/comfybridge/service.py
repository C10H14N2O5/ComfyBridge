"""Bounded local admission and conservative, durable remote task ownership."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import time
import uuid
from typing import TYPE_CHECKING

from .audio import AudioError, AudioProcessor, AudioResult
from .cache_scheduler import CacheScheduler
from .comfy_client import ComfyClient, ComfyError, SubmissionUncertain, SubmissionRejected
from .config import AppConfig
from .journal import Journal, TERMINAL
from .logging_utils import event
from .workflows import load_presets, validate_live

if TYPE_CHECKING:
    from .api import SpeechRequest


class BridgeError(Exception):
    def __init__(self, code: str, status: int = 502, *, param: str | None = None, submitted: bool = False):
        self.code, self.status, self.param, self.submitted = code, status, param, submitted
        super().__init__(code)


@dataclass
class _Work:
    request: object
    request_id: str
    deadline: float
    future: asyncio.Future
    preset: object
    task_id: str | None = None
    abandoned: bool = False
    slot: bool = False
    result: AudioResult | None = None
    sequence: int = 0
    abandon_error: BridgeError | None = None


class BridgeService:
    def __init__(self, config: AppConfig, data_dir: Path, *, audio_processor=None,
                 client=None, client_factory=None, presets=None, journal=None, clock=None, wall_clock=None):
        self.config = config.model_copy(deep=True)
        self.data_dir = Path(data_dir)
        self.presets = load_presets(self.config, self.data_dir) if presets is None else presets
        self.audio = audio_processor if audio_processor is not None else AudioProcessor(self.config.limits)
        self.journal = journal or Journal(self.data_dir / "runtime" / "tasks.sqlite3")
        self._client_factory = client_factory or (lambda url: ComfyClient(url, self.config.limits))
        self._clients = {}
        if client is not None:
            self._clients[self.config.comfyui.base_url.rstrip("/")] = client
        self._queue = deque()
        self._sequence = 0
        self._slots = 0
        self._delivery = 0
        self._delivered_results = {}
        self._workers = set()
        self._remote = {}
        self._live = {}
        self._ready = {key: ["not_probed"] for key in self.presets}
        self._online = False
        self._draining = False
        self._started = False
        self._closed = False
        self._journal_closed = False
        self._maintenance = None
        self._wake = asyncio.Event()
        self._config_version = hashlib.sha256(self.config.model_dump_json(exclude={"server": {"api_key"}}).encode()).hexdigest()
        self.cache = CacheScheduler(self, clock=clock, wall_clock=wall_clock)

    def _client(self, url: str):
        url = url.rstrip("/")
        if url not in self._clients:
            self._clients[url] = self._client_factory(url)
        return self._clients[url]

    def _blocked(self, url: str | None = None, *, business_only: bool = False) -> bool:
        target = (url or self.config.comfyui.base_url).rstrip("/")
        return any(row["backend_url"].rstrip("/") == target and row["state"] in {"intent", "uncertain"}
                   and (not business_only or row["purpose"] == "speech") for row in self.journal.unresolved())

    async def start(self):
        if self._started:
            return
        self._started = True
        # Every prior HTTP request is gone; no prior local delivery remains active.
        # Only UUID directories actually recorded in this ledger are owned.
        for task_id in self.journal.known_task_ids():
            self._cleanup_directory(self.data_dir / "tmp" / ("cb-" + task_id))
        self.cache.recover()
        for row in self.journal.unresolved():
            state = "uncertain" if row["state"] == "intent" else row["state"]
            client_state = row["client_state"] if row["purpose"] == "cache_cleanup" else "abandoned"
            row = self.journal.update(row["task_id"], state=state, client_state=client_state, absent_count=0)
            self._slots += 1
            self._remote[row["task_id"]] = None
            self._spawn(self._follow(row["task_id"], None))
        self.journal.prune()
        await self._probe()
        self._maintenance = asyncio.create_task(self._maintain(), name="bridge-maintenance")
        self._maintenance.add_done_callback(self._worker_finished)
        event("service_started", instance_id=self.journal.instance_id, count=len(self._remote))

    def _cleanup_directory(self, directory: Path):
        root = self.data_dir / "tmp"
        try:
            if (root.is_symlink() or root.is_junction() or directory.is_symlink() or directory.is_junction()
                    or directory.parent.resolve() != root.resolve() or not directory.is_dir()):
                return
            for name in ("source.audio", "output.mp3", "output.wav"):
                path = directory / name
                if path.is_file() and not path.is_symlink() and not path.is_junction():
                    path.unlink()
            directory.rmdir()  # Never recurse, never remove unknown extra contents.
        except OSError:
            event("cleanup_deferred", code="local_cleanup_failed")

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._workers.add(task)
        task.add_done_callback(self._worker_finished)
        return task

    def _worker_finished(self, task):
        self._workers.discard(task)
        if not task.cancelled() and task.exception() is not None:
            # Consume the exception; asyncio's default handler could reveal upstream text.
            self._online = False
            self._draining = True
            event("worker_failed", code="internal_error")

    async def _probe(self):
        try:
            classes = {node["class_type"] for loaded in self.presets.values() for node in loaded.graph.values()}
            schemas = await self._client(self.config.comfyui.base_url).schemas(classes)
            self._ready = {key: validate_live(loaded, schemas) for key, loaded in self.presets.items()}
            self._online = True
        except Exception:
            # Never expose exception strings: third-party responses may contain prompt text.
            self._online = False
            self._ready = {key: ["remote_unavailable"] for key in self.presets}

    async def _maintain(self):
        next_probe = time.monotonic() + self.config.limits.probe_interval
        try:
            # Cancellation can race a completed wait. The closed flag must also
            # terminate this loop so close() cannot hang on the maintenance task.
            while not self._closed:
                self._dispatch()
                if time.monotonic() >= next_probe:
                    await self._probe()
                    self.journal.prune(protected_ids=set(self._remote) | set(self._delivered_results))
                    self.cache.prune_retired()
                    next_probe = time.monotonic() + self.config.limits.probe_interval
                    self._dispatch()
                self.cache.tick()
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=min(self.config.limits.poll_interval, max(0.01, next_probe - time.monotonic())))
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    def _dispatch(self):
        if self.cache.busy:
            return
        while self._queue and self._slots < self.config.limits.max_inflight:
            work = self._queue[0]
            if work.abandoned or work.future.done():
                self._queue.popleft()
                continue
            if time.monotonic() >= work.deadline:
                self._queue.popleft()
                self._fail(work, BridgeError("deadline_exceeded", 504))
                continue
            if self._blocked() or not self._online or self._ready.get((work.request.model, work.request.voice)):
                break
            self._queue.popleft()
            work.slot = True
            self._slots += 1
            self._spawn(self._execute(work))

    def status(self) -> dict:
        rows = self.journal.unresolved()
        return {
            "online": self._online, "ready": self._online and not self._draining and not self._blocked() and any(not codes for codes in self._ready.values()),
            "draining": self._draining, "inflight": len(self._remote), "slots": self._slots,
            "waiting": sum(not work.abandoned and not work.future.done() for work in self._queue),
            "delivery": self._delivery, "live_requests": len(self._live),
            "cache": self.cache.status(),
            "presets": [{"model": key[0], "voice": key[1], "ready": self._online and not codes,
                         "reasons": list(codes)} for key, codes in self._ready.items()],
            "unresolved": [{"task_id": row["task_id"], "request_id": row["request_id"], "state": row["state"],
                            "client_state": row["client_state"], "error_code": row["error_code"], "purpose": row["purpose"]} for row in rows],
        }

    def begin_drain(self):
        self._draining = True

    def is_drained(self) -> bool:
        return not self._live and self._slots == 0 and not self.journal.unresolved()

    async def resolve_uncertain(self, task_id: str):
        if task_id in self._remote:
            row = self.journal.get(task_id)
            # A POST in progress may still be accepted after acknowledgment; never resolve it.
            if row["state"] == "intent":
                raise BridgeError("submission_in_progress", 409)
        try:
            self.journal.acknowledge(task_id)
        except (KeyError, ValueError):
            raise BridgeError("task_not_uncertain", 409) from None
        self._wake.set()

    async def synthesize(self, request: SpeechRequest, request_id: str, disconnected=None) -> AudioResult:
        if not self._started or self._closed or self._draining:
            raise BridgeError("service_draining", 503)
        loaded = self.presets.get((request.model, request.voice))
        if loaded is None:
            raise BridgeError("unknown_preset", 400, param="voice")
        if self._blocked(business_only=True):
            raise BridgeError("unresolved_submission", 503, submitted=True)
        if not self._online or self._ready.get((request.model, request.voice)):
            raise BridgeError("preset_unready", 503)
        waiting = sum(not w.abandoned and not w.future.done() for w in self._queue)
        effective_slots = self._slots - (1 if self.cache.preflight else 0)
        if ((self.cache.busy and not self.cache.preflight and waiting >= self.config.limits.max_queue)
                or effective_slots + waiting >= self.config.limits.max_inflight + self.config.limits.max_queue):
            raise BridgeError("capacity_exceeded", 429)
        self.cache.activity()
        future = asyncio.get_running_loop().create_future()
        deadline = getattr(request, "_deadline", None) or time.monotonic() + self.config.limits.request_timeout
        work = _Work(request, request_id, deadline, future, loaded, sequence=self._sequence)
        self._sequence += 1
        token = str(uuid.uuid4())
        self._live[token] = work
        self._queue.append(work)
        self._dispatch()
        self._wake.set()
        try:
            while not future.done():
                if time.monotonic() >= work.deadline:
                    self._abandon(work, "deadline_exceeded", 504)
                    break
                if disconnected is not None:
                    async with asyncio.timeout(max(0.001, work.deadline - time.monotonic())):
                        gone = await disconnected()
                    if gone:
                        self._abandon(work, "client_disconnected", 499)
                        break
                await asyncio.wait({future}, timeout=min(0.1, max(0.001, work.deadline - time.monotonic())))
            if work.abandoned and work.result is not None:
                await work.result.cleanup()
                raise work.abandon_error or BridgeError("client_disconnected", 499, submitted=work.task_id is not None)
            return future.result()
        except asyncio.CancelledError:
            self._abandon(work, "client_disconnected", 499)
            if work.result is not None:
                await work.result.cleanup()
            # Consume locally generated future failure even if caller no longer awaits it.
            if future.done() and not future.cancelled():
                future.exception()
            raise
        except asyncio.TimeoutError:
            self._abandon(work, "deadline_exceeded", 504)
            if work.result is not None:
                await work.result.cleanup()
            if future.done() and not future.cancelled():
                future.exception()
            raise BridgeError("deadline_exceeded", 504, submitted=work.task_id is not None) from None
        except Exception as error:
            # Disconnect probes and transports can fail independently of cancellation.
            # No orphaned future may later retain a delivery slot and private files.
            self._abandon(work, "client_disconnected", 499)
            if work.result is not None:
                await work.result.cleanup()
            if future.done() and not future.cancelled():
                future.exception()
            if isinstance(error, BridgeError):
                raise
            raise BridgeError("request_tracking_failed", 502, submitted=work.task_id is not None) from None
        finally:
            self._live.pop(token, None)
            self._wake.set()

    def _fail(self, work: _Work, error: BridgeError):
        if not work.future.done():
            work.future.set_exception(error)

    def _abandon(self, work: _Work, code: str, status: int):
        work.abandoned = True
        if work.abandon_error is None:
            work.abandon_error = BridgeError(code, status, submitted=work.task_id is not None)
        if work.task_id and not self._journal_closed:
            self.journal.update(work.task_id, client_state="abandoned")
        # Remove dead waiters immediately even while all remote slots are stuck.
        # Otherwise repeated timed-out waiters could retain unbounded private inputs.
        if work.task_id is None:
            self._queue = deque(item for item in self._queue if item is not work)
        self._fail(work, work.abandon_error)
        self._wake.set()

    def _release(self, work: _Work | None):
        if work is None or work.slot:
            self._slots -= 1
            if work is not None:
                work.slot = False
            self._wake.set()

    async def _execute(self, work: _Work):
        task_id = str(uuid.uuid4())
        try:
            if work.abandoned or time.monotonic() >= work.deadline:
                self._fail(work, BridgeError("deadline_exceeded", 504))
                self._release(work)
                return
            # Reserved local workers may have been scheduled before an earlier POST
            # became uncertain. Recheck at the actual submission boundary.
            if self.cache.busy or self._blocked() or not self._online or self._ready.get((work.request.model, work.request.voice)):
                self._queue = deque(sorted((*self._queue, work), key=lambda item: item.sequence))
                self._release(work)
                return
            # Build before intent: local failures cannot create remote work.
            graph = work.preset.build_prompt(work.request.input)
            row = self.journal.intent(task_id=task_id, request_id=work.request_id,
                                      backend_url=self.config.comfyui.base_url,
                                      template_version=work.preset.preset.sha256,
                                      config_version=self._config_version, model=work.request.model,
                                      voice=work.request.voice, preset_id=getattr(work.preset.preset, "id", None),
                                      cache_use=self.cache.metadata(work.preset))
            work.task_id = task_id
            self._remote[task_id] = work
            event("submission_intent", prompt_id=task_id, request_id=work.request_id,
                  model=work.request.model, voice=work.request.voice, input_length=len(work.request.input))
            try:
                async with asyncio.timeout(max(0.001, min(self.config.limits.submit_timeout, work.deadline - time.monotonic()))):
                    await self._client(row["backend_url"]).submit(graph, task_id, row["instance_id"], row["request_id"])
                self.journal.update(task_id, state="pending", admitted=1)
                event("submission_acknowledged", prompt_id=task_id, request_id=work.request_id)
            except SubmissionRejected:
                self.journal.update(task_id, state="error", error_code="submission_rejected")
                self._fail(work, BridgeError("submission_rejected", 502, submitted=True))
                event("submission_rejected", prompt_id=task_id)
            except (SubmissionUncertain, asyncio.TimeoutError):
                self.journal.update(task_id, state="uncertain", error_code="submission_uncertain")
                event("submission_uncertain", prompt_id=task_id)
                # Keep the original caller while reconciling, but never replay this POST.
            finally:
                del graph
            await self._follow(task_id, work)
        except asyncio.CancelledError:
            # Force exit never declares a possibly unfinished remote task complete.
            if work.task_id:
                self.journal.update(task_id, client_state="abandoned")
            raise
        except Exception:
            if work.task_id and self.journal.get(task_id)["state"] not in TERMINAL:
                self.journal.update(task_id, state="uncertain", client_state="abandoned", error_code="internal_error")
                self._abandon(work, "internal_error", 502)
                # Preserve capacity and continue conservative recovery after an unexpected failure.
                await self._follow(task_id, work)
            else:
                self._fail(work, BridgeError("internal_error", 502, submitted=bool(work.task_id)))
                self._release(work)

    async def _follow(self, task_id: str, work: _Work | None):
        try:
            while True:
                row = self.journal.get(task_id)
                if row["state"] in TERMINAL:
                    break
                row = self.cache.expire(row)
                if work is not None and not work.abandoned and time.monotonic() >= work.deadline:
                    self._abandon(work, "deadline_exceeded", 504)
                client = self._client(row["backend_url"])
                delay = self.config.limits.poll_interval
                try:
                    observed = await client.observe(row["remote_id"])
                    if self.journal.get(task_id)["state"] in TERMINAL:
                        break  # A local admin acknowledgment must not be overwritten.
                    if observed.state == "absent" and row["state"] in {"uncertain", "intent"}:
                        found = await client.correlate(row["instance_id"], row["request_id"])
                        if self.journal.get(task_id)["state"] in TERMINAL:
                            break
                        if found is not None:
                            self.journal.update(task_id, remote_id=found, admitted=1, absent_count=0)
                            observed = await client.observe(found)
                            row = self.journal.get(task_id)
                            if row["state"] in TERMINAL:
                                break
                    if observed.state in {"success", "error"}:
                        self.journal.update(task_id, state=observed.state, admitted=1, absent_count=0,
                                            error_code="remote_execution_failed" if observed.state == "error" else None)
                        event("remote_terminal", prompt_id=row["remote_id"], state=observed.state)
                        if work is not None and not work.abandoned:
                            if observed.state == "success":
                                await self._deliver(work, client, observed.history)
                            else:
                                self._fail(work, BridgeError("remote_execution_failed", 502, submitted=True))
                        break
                    if observed.state in {"pending", "running"}:
                        row = self.journal.update(task_id, state=observed.state, admitted=1, absent_count=0, error_code=None)
                        cancel_pending = bool(row["expired"]) if row["purpose"] == "cache_cleanup" else (work is None or work.abandoned)
                        if observed.state == "pending" and cancel_pending:
                            self.journal.update(task_id, delete_requested=1, absent_count=0)
                            event("pending_delete_requested", prompt_id=row["remote_id"])
                            await client.delete_pending(row["remote_id"])
                            # A lost or non-effective response can be retried ONLY after
                            # another fresh observation of this same owned ID pending.
                    elif observed.state == "absent":
                        if row["admitted"] and row["delete_requested"]:
                            count = row["absent_count"] + 1
                            self.journal.update(task_id, absent_count=count)
                            if count >= 2:
                                self.journal.update(task_id, state="not_active", error_code="remote_not_active")
                                break
                        else:
                            self.journal.update(task_id, state="uncertain", absent_count=0, error_code="remote_state_unknown")
                    else:
                        raise ComfyError("remote_protocol_error")
                except ComfyError:
                    if self.journal.get(task_id)["state"] in TERMINAL:
                        break
                    self.journal.update(task_id, state="uncertain", absent_count=0, error_code="remote_unavailable")
                    if row["backend_url"].rstrip("/") == self.config.comfyui.base_url.rstrip("/"):
                        self._online = False
                    delay = self.config.limits.probe_interval
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            # Only terminal evidence or admin acknowledgment releases remote capacity.
            if self.journal.get(task_id)["state"] in TERMINAL:
                self._remote.pop(task_id, None)
                self.cache.finished(task_id)
                if work is None or work.result is None:
                    self._release(work)
                if work is not None and not work.future.done():
                    self._fail(work, BridgeError("remote_not_active", 502, submitted=True))

    async def _deliver(self, work: _Work, client, history):
        directory = self.data_dir / "tmp" / ("cb-" + work.task_id)
        source = directory / "source.audio"
        destination = directory / ("output." + work.request.response_format)
        created = False
        try:
            descriptor = client.audio_descriptor(history, work.preset.preset.audio_output_node)
            root = directory.parent
            root.mkdir(parents=True, exist_ok=True)
            if root.is_symlink() or root.is_junction() or root.resolve().parent != self.data_dir.resolve():
                raise BridgeError("unsafe_temp_directory", 502, submitted=True)
            directory.mkdir(exist_ok=False)
            created = True
            await client.download(descriptor, source, work.deadline)
            if work.abandoned:
                return
            async with asyncio.timeout(max(0.001, work.deadline - time.monotonic())):
                result = await self.audio.convert(source, destination, work.request.response_format, work.deadline)
            if work.abandoned or time.monotonic() >= work.deadline:
                await result.cleanup()
                if not work.abandoned:
                    self._abandon(work, "deadline_exceeded", 504)
                return
            original = result.cleanup_callback
            async def cleanup():
                try:
                    if original:
                        returned = original()
                        if inspect.isawaitable(returned):
                            await returned
                finally:
                    self._delivery -= 1
                    self._delivered_results.pop(work.task_id, None)
                    if not self._closed:
                        completed = bool(getattr(result, "_delivery_completed", False)) and not work.abandoned
                        self.journal.update(work.task_id, client_state="delivered" if completed else "abandoned")
                    self._release(work)
            result.cleanup_callback = cleanup
            self._delivery += 1
            work.result = result
            self._delivered_results[work.task_id] = result
            work.future.set_result(result)
        except (AudioError, ComfyError) as error:
            code = error.code
            # These are internal allow-listed exceptions, not upstream response messages.
            self._fail(work, BridgeError(code, 504 if code in {"deadline_exceeded", "audio_timeout"} else 502, submitted=True))
        except asyncio.TimeoutError:
            self._fail(work, BridgeError("deadline_exceeded", 504, submitted=True))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail(work, BridgeError("audio_processing_failed", 502, submitted=True))
        finally:
            if work.result is None and created:
                self._cleanup_directory(directory)

    async def close(self, force: bool = False):
        if self._closed:
            return
        self.begin_drain()
        if not force:
            while not self.is_drained():
                await asyncio.sleep(0.05)
        self._closed = True
        if self._maintenance:
            self._maintenance.cancel()
            await asyncio.gather(self._maintenance, return_exceptions=True)
        for work in list(self._live.values()):
            self._abandon(work, "service_stopped", 503)
        for task in list(self._workers):
            task.cancel()
        await asyncio.gather(*list(self._workers), return_exceptions=True)
        for result in list(self._delivered_results.values()):
            await result.cleanup()
        for client in self._clients.values():
            await client.close()
        self._journal_closed = True
        self.journal.close()
