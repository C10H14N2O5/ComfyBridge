import asyncio
import json
import time
import uuid

import httpx
import pytest

from comfybridge.comfy_client import ComfyClient, ComfyError, SubmissionUncertain, SubmissionRejected
from comfybridge.config import LimitsConfig


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lost", "redirect", "mismatch", "nonjson", "server_error"])
async def test_submission_is_single_attempt_and_uncertain(mode):
    calls = []
    prompt_id = str(uuid.uuid4())
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert payload["prompt_id"] == prompt_id
        assert payload["extra_data"]["comfybridge"] == {"instance_id": "instance", "request_id": "request"}
        if mode == "lost":
            raise httpx.ReadTimeout("sensitive input text", request=request)
        if mode == "redirect":
            return httpx.Response(307, headers={"location": "/prompt"})
        if mode == "mismatch":
            return httpx.Response(200, json={"prompt_id": str(uuid.uuid4())})
        if mode == "nonjson":
            return httpx.Response(200, text="sensitive text")
        return httpx.Response(500, text="sensitive text")
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(SubmissionUncertain) as raised:
        await client.submit({"text": "private"}, prompt_id, "instance", "request")
    assert str(raised.value) == "submission_uncertain"
    assert len(calls) == 1
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"{}", b"not-json"])
async def test_free_posts_once_and_accepts_native_empty_ack(body):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path == "/prefix/free"
        assert json.loads(request.content) == {"unload_models": True, "free_memory": True}
        return httpx.Response(200, content=body)
    client = ComfyClient("http://mock.invalid/prefix", LimitsConfig(), transport=httpx.MockTransport(handler))
    try:
        assert await client.free() is None
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "network", "os", "redirect", "server_error", "not_found"])
async def test_free_failure_is_sanitized_never_retried(mode):
    calls = []
    def handler(request):
        calls.append(request)
        if mode == "timeout":
            raise httpx.ReadTimeout("private remote data", request=request)
        if mode == "network":
            raise httpx.ConnectError("private remote data", request=request)
        if mode == "os":
            raise OSError("private remote data")
        return httpx.Response({"redirect": 307, "server_error": 500, "not_found": 404}[mode],
                              headers={"location": "/free"}, text="private remote data")
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ComfyError) as caught:
            await client.free()
        assert str(caught.value) == ("cache_free_uncertain" if mode in {"timeout", "network", "os"} else "cache_free_http_error")
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_free_is_bounded_and_cancellation_propagates():
    calls = []
    async def handler(request):
        calls.append(request)
        await asyncio.Event().wait()
    client = ComfyClient("http://mock.invalid", LimitsConfig(query_timeout=0.02), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ComfyError, match="cache_free_uncertain"):
            await asyncio.wait_for(client.free(), timeout=1)
        assert len(calls) == 1
        task = asyncio.create_task(client.free())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_native_failure_completed_false_is_terminal():
    task_id = str(uuid.uuid4())
    def handler(request):
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [[1, task_id]], "queue_pending": []})
        return httpx.Response(200, json={task_id: {"status": {"status_str": "error", "completed": False}, "messages": ["sensitive text"]}})
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    assert (await client.observe(task_id)).state == "error"
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [{}, {"status_str": "running"}, {"completed": False}, {"status_str": None}, {"status_str": []}])
async def test_owned_unknown_history_never_counts_as_healthy_absence(status):
    task_id = str(uuid.uuid4())
    def handler(request):
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(200, json={task_id: {"status": status}})
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    for _ in range(2):
        with pytest.raises(ComfyError, match="remote_protocol_error"):
            await client.observe(task_id)
    await client.close()


@pytest.mark.asyncio
async def test_missing_history_is_absent_not_canceled_and_safe_delete():
    calls = []
    task_id = str(uuid.uuid4())
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert request.url.path == "/queue"
            assert json.loads(request.content) == {"delete": [task_id]}
            return httpx.Response(200)
        return httpx.Response(200, json={"queue_running": [], "queue_pending": []} if request.url.path == "/queue" else {})
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    assert (await client.observe(task_id)).state == "absent"
    await client.delete_pending(task_id)
    assert (await client.observe(task_id)).state == "absent"
    assert not any("interrupt" in str(call.url) or "jobs" in str(call.url) for call in calls)
    await client.close()


@pytest.mark.asyncio
async def test_correlation_uses_exact_instance_and_request_only():
    task_id = str(uuid.uuid4())
    unrelated = [0, str(uuid.uuid4()), {}, {"comfybridge": {"instance_id": "other", "request_id": "request"}}]
    owned = [1, task_id, {}, {"comfybridge": {"instance_id": "instance", "request_id": "request"}}]
    def handler(request):
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [unrelated], "queue_pending": []})
        assert request.url.params["max_items"] == "1000"
        return httpx.Response(200, json={task_id: {"prompt": owned}})
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    assert await client.correlate("instance", "request") == task_id
    assert await client.correlate("instance", "not-request") is None
    await client.close()


@pytest.mark.asyncio
async def test_malformed_or_oversized_json_fails_closed(monkeypatch):
    import comfybridge.comfy_client as module
    monkeypatch.setattr(module, "MAX_JSON_BYTES", 20)
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 21)))
    with pytest.raises(ComfyError, match="remote_response_too_large"):
        await client.queue()
    await client.close()


@pytest.mark.parametrize("descriptor", [
    {"filename": "../a.wav", "subfolder": "", "type": "output"},
    {"filename": "a.wav", "subfolder": "../other", "type": "output"},
    {"filename": "C:secret", "subfolder": "", "type": "output"},
    {"filename": "a.wav", "subfolder": "/absolute", "type": "output"},
    {"filename": "a.wav", "subfolder": "", "type": "input"},
    {"filename": "a\x00.wav", "subfolder": "", "type": "output"},
    {"filename": "a.wav", "subfolder": ".. /input", "type": "output"},
    {"filename": "a.wav", "subfolder": "directory./child", "type": "output"},
    {"filename": "NUL.wav", "subfolder": "", "type": "output"},
    {"filename": "a.wav", "subfolder": "COM1", "type": "output"},
    {"filename": "a.wav ", "subfolder": "", "type": "output"},
])
def test_descriptor_validation(descriptor):
    with pytest.raises(ComfyError):
        ComfyClient.audio_descriptor({"outputs": {"save": {"audio": [descriptor]}}}, "save")


def test_only_exact_selected_node_and_one_audio():
    good = {"filename": "音频.wav", "subfolder": "voice/day", "type": "output"}
    assert ComfyClient.audio_descriptor({"outputs": {"save": {"audio": [good]}, "other": {"audio": []}}}, "save") == good
    for history in ({"outputs": {"other": {"audio": [good]}}}, {"outputs": {"save": {"audio": [good, good]}}}):
        with pytest.raises(ComfyError):
            ComfyClient.audio_descriptor(history, "save")


@pytest.mark.asyncio
async def test_safe_native_rejection_is_not_uncertain():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"type": "prompt_outputs_failed_validation", "message": "private text"}, "node_errors": {"1": {"errors": ["private"]}}})
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(SubmissionRejected) as error:
        await client.submit({}, str(uuid.uuid4()), "instance", "request")
    assert str(error.value) == "submission_rejected"
    assert len(calls) == 1
    await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("submit", [False, True])
async def test_slow_trickle_is_bounded_by_total_timeout(submit):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'
            for _ in range(30):
                await asyncio.sleep(0.01)
                yield b' '
            yield b'}'
    client = ComfyClient("http://mock.invalid", LimitsConfig(query_timeout=0.04, submit_timeout=0.04),
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=SlowStream())))
    start = time.monotonic()
    with pytest.raises(SubmissionUncertain if submit else ComfyError):
        if submit:
            await client.submit({}, str(uuid.uuid4()), "instance", "request")
        else:
            await client.queue()
    assert time.monotonic() - start < 0.2
    await client.close()


@pytest.mark.asyncio
async def test_correlation_aggregate_bytes_bounded(monkeypatch):
    import comfybridge.comfy_client as module
    monkeypatch.setattr(module, "MAX_JSON_BYTES", 80)
    def handler(request):
        if request.url.path == "/queue":
            return httpx.Response(200, content=b'{"queue_running":[],"queue_pending":[]}' + b' ' * 20)
        return httpx.Response(200, content=b'{}' + b' ' * 30)
    client = ComfyClient("http://mock.invalid", LimitsConfig(), transport=httpx.MockTransport(handler))
    with pytest.raises(ComfyError, match="remote_response_too_large"):
        await client.correlate("instance", "request")
    await client.close()


@pytest.mark.asyncio
async def test_download_cap_and_deadline(tmp_path):
    limits = LimitsConfig(max_audio_bytes=4)
    client = ComfyClient("http://mock.invalid", limits, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"12345")))
    with pytest.raises(ComfyError, match="audio_too_large"):
        await client.download({"filename": "x.wav", "subfolder": "", "type": "output"}, tmp_path / "audio", time.monotonic() + 10)
    with pytest.raises(ComfyError, match="deadline_exceeded"):
        await client.download({}, tmp_path / "late", time.monotonic() - 1)
    await client.close()
