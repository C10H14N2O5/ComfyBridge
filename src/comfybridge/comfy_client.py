"""Small allow-listed ComfyUI protocol client; never retries a submission."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote
import asyncio
import ntpath
import time
import uuid

import httpx

from .config import parse_json_strict

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_CORRELATION_ENTRIES = 1000


class ComfyError(Exception):
    def __init__(self, code: str = "remote_unavailable"):
        self.code = code
        super().__init__(code)


class SubmissionUncertain(ComfyError):
    def __init__(self):
        super().__init__("submission_uncertain")


class SubmissionRejected(ComfyError):
    def __init__(self):
        super().__init__("submission_rejected")


@dataclass(frozen=True)
class Observation:
    state: str  # pending, running, success, error, absent
    history: dict | None = None


class ComfyClient:
    def __init__(self, base_url: str, limits, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self.limits = limits
        self.http = httpx.AsyncClient(base_url=self.base_url + "/", follow_redirects=False,
                                     trust_env=False, transport=transport,
                                     timeout=httpx.Timeout(limits.query_timeout, connect=limits.connect_timeout))

    async def close(self) -> None:
        await self.http.aclose()

    async def _json(self, method: str, route: str, *, payload=None, timeout=None, submission=False, byte_budget=None) -> dict:
        budget = timeout or self.limits.query_timeout
        try:
            # httpx read timeouts reset on each chunk; the outer timeout bounds the whole call.
            async with asyncio.timeout(budget):
                async with self.http.stream(method, route, json=payload,
                        timeout=httpx.Timeout(budget, connect=min(budget, self.limits.connect_timeout))) as response:
                    if response.status_code != 200 and not (submission and response.status_code == 400):
                        raise ComfyError("remote_http_error")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if byte_budget is not None:
                            byte_budget[0] -= len(chunk)
                        if len(data) > MAX_JSON_BYTES or (byte_budget is not None and byte_budget[0] < 0):
                            raise ComfyError("remote_response_too_large")
                    parsed = parse_json_strict(bytes(data))
                    if not isinstance(parsed, dict):
                        raise ComfyError("remote_protocol_error")
                    if response.status_code == 400:
                        if ("prompt_id" not in parsed and isinstance(parsed.get("error"), dict)
                                and isinstance(parsed["error"].get("type"), str)
                                and isinstance(parsed.get("node_errors"), dict)):
                            raise SubmissionRejected()
                        raise ComfyError("remote_protocol_error")
                    return parsed
        except ComfyError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError, OSError, RecursionError, asyncio.TimeoutError):
            raise ComfyError("remote_unavailable") from None

    async def schemas(self, class_types: set[str]) -> dict:
        result = {}
        for class_type in sorted(class_types):
            document = await self._json("GET", "object_info/" + quote(class_type, safe=""))
            if isinstance(document.get(class_type), dict):
                result[class_type] = document[class_type]
        # Even an empty preset registry must report connectivity accurately.
        if not class_types:
            await self.queue()
        return result

    async def submit(self, graph: dict, prompt_id: str, instance_id: str, request_id: str) -> None:
        """Exactly one transport attempt. A UUID is correlation, NOT a deduplication key."""
        try:
            answer = await self._json("POST", "prompt", payload={
                "prompt": graph, "prompt_id": prompt_id,
                "extra_data": {"comfybridge": {"instance_id": instance_id, "request_id": request_id}},
            }, timeout=self.limits.submit_timeout, submission=True)
            if answer.get("prompt_id") != prompt_id or answer.get("node_errors"):
                raise SubmissionUncertain()
        except SubmissionRejected:
            raise
        except (ComfyError, asyncio.TimeoutError):
            raise SubmissionUncertain() from None

    async def queue(self, *, byte_budget=None) -> dict:
        document = await self._json("GET", "queue", byte_budget=byte_budget)
        for key in ("queue_running", "queue_pending"):
            entries = document.get(key)
            if not isinstance(entries, list):
                raise ComfyError("remote_protocol_error")
            if len(entries) > MAX_CORRELATION_ENTRIES:
                raise ComfyError("remote_response_too_large")
            for entry in entries:
                if not isinstance(entry, list) or len(entry) < 2 or not isinstance(entry[1], str):
                    raise ComfyError("remote_protocol_error")
        return document

    async def observe(self, prompt_id: str) -> Observation:
        # Query history AFTER queue. A terminal history result wins over a stale queue.
        queue = await self.queue()
        document = await self._json("GET", "history/" + quote(prompt_id, safe=""))
        if document and set(document) != {prompt_id}:
            raise ComfyError("remote_protocol_error")
        history = document.get(prompt_id)
        if prompt_id in document:
            if not isinstance(history, dict) or not isinstance(history.get("status"), dict):
                raise ComfyError("remote_protocol_error")
            state = history["status"].get("status_str")
            if isinstance(state, str) and state in {"success", "error"}:
                # Native failure can have completed=false; do not wait forever on it.
                return Observation(state, history)
            # An owned history record with an unknown/malformed status is evidence
            # of uncertainty, not a healthy observation that this task is absent.
            raise ComfyError("remote_protocol_error")
        if any(entry[1] == prompt_id for entry in queue["queue_running"]):
            return Observation("running")
        if any(entry[1] == prompt_id for entry in queue["queue_pending"]):
            return Observation("pending")
        return Observation("absent")

    async def correlate(self, instance_id: str, request_id: str) -> str | None:
        byte_budget = [MAX_JSON_BYTES]
        queue = await self.queue(byte_budget=byte_budget)
        history = await self._json("GET", "history?max_items=1000", byte_budget=byte_budget)
        if len(history) > MAX_CORRELATION_ENTRIES:
            raise ComfyError("remote_response_too_large")
        entries = queue["queue_running"] + queue["queue_pending"]
        entries += [item.get("prompt") for item in history.values() if isinstance(item, dict)]
        if len(entries) > MAX_CORRELATION_ENTRIES:
            raise ComfyError("remote_response_too_large")
        matches = set()
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 4 or not isinstance(entry[3], dict):
                continue
            tag = entry[3].get("comfybridge")
            if isinstance(tag, dict) and tag.get("instance_id") == instance_id and tag.get("request_id") == request_id:
                try:
                    candidate = str(uuid.UUID(entry[1]))
                except (TypeError, ValueError, AttributeError):
                    raise ComfyError("remote_protocol_error") from None
                if candidate != entry[1]:
                    raise ComfyError("remote_protocol_error")
                matches.add(candidate)
        if len(matches) > 1:
            raise ComfyError("ambiguous_correlation")
        return next(iter(matches), None)

    async def delete_pending(self, prompt_id: str) -> None:
        # Called only after the service observes this owned ID pending.
        # Native /queue may return an empty 200 body. It is not proof of deletion.
        try:
            async with asyncio.timeout(self.limits.query_timeout):
                async with self.http.stream("POST", "queue", json={"delete": [prompt_id]},
                        timeout=httpx.Timeout(self.limits.query_timeout, connect=self.limits.connect_timeout)) as response:
                    if response.status_code != 200:
                        raise ComfyError("remote_http_error")
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise ComfyError("remote_unavailable") from None

    @staticmethod
    def audio_descriptor(history: dict, output_node: str) -> dict:
        outputs = history.get("outputs")
        selected = outputs.get(output_node) if isinstance(outputs, dict) else None
        audio = selected.get("audio") if isinstance(selected, dict) else None
        if not isinstance(audio, list) or len(audio) != 1 or not isinstance(audio[0], dict):
            raise ComfyError("invalid_audio_output")
        item = audio[0]
        filename, subfolder, kind = item.get("filename"), item.get("subfolder", ""), item.get("type")
        if (not isinstance(filename, str) or not filename or filename in {".", ".."}
                or any(c in filename for c in '/\\:') or any(ord(c) < 32 for c in filename)
                or not isinstance(subfolder, str) or '\\' in subfolder or ':' in subfolder
                or any(ord(c) < 32 for c in subfolder)
                or (subfolder and any(part in {"", ".", ".."} for part in subfolder.split('/')))
                or not isinstance(kind, str) or kind not in {"output", "temp"}):
            raise ComfyError("invalid_audio_descriptor")
        if any(part.endswith((" ", ".")) or ntpath.isreserved(part)
               for part in [filename, *subfolder.split("/")] if part):
            raise ComfyError("invalid_audio_descriptor")
        try:
            filename.encode("utf-8")
            subfolder.encode("utf-8")
        except UnicodeError:
            raise ComfyError("invalid_audio_descriptor") from None
        return {"filename": filename, "subfolder": subfolder, "type": kind}

    async def download(self, descriptor: dict, destination: Path, deadline: float) -> None:
        remaining = min(self.limits.download_timeout, deadline - time.monotonic())
        if remaining <= 0:
            raise ComfyError("deadline_exceeded")
        try:
            async with asyncio.timeout(remaining):
                async with self.http.stream("GET", "view", params=descriptor,
                        timeout=httpx.Timeout(remaining, connect=min(remaining, self.limits.connect_timeout))) as response:
                    if response.status_code != 200:
                        raise ComfyError("audio_download_failed")
                    count = 0
                    with destination.open("xb") as output:
                        async for chunk in response.aiter_bytes():
                            count += len(chunk)
                            if count > self.limits.max_audio_bytes:
                                raise ComfyError("audio_too_large")
                            output.write(chunk)
                    if count == 0:
                        raise ComfyError("empty_audio")
        except asyncio.TimeoutError:
            raise ComfyError("deadline_exceeded") from None
        except (httpx.HTTPError, OSError):
            raise ComfyError("audio_download_failed") from None
