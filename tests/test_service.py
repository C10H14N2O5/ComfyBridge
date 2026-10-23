import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

from comfybridge.api import SpeechRequest
from comfybridge.audio import AudioResult, AudioError
from comfybridge.comfy_client import ComfyClient, ComfyError, Observation, SubmissionUncertain, SubmissionRejected
from comfybridge.config import AppConfig, ComfyConfig, LimitsConfig, ModelConfig, ServerConfig
from comfybridge.journal import Journal
from comfybridge.service import BridgeError, BridgeService


class Preset:
    def __init__(self, name="voice"):
        self.preset = SimpleNamespace(sha256="a" * 64, audio_output_node="save", voice=name)
        self.graph = {"tts": {"class_type": "TTS", "inputs": {"text": "template"}}}
    def build_prompt(self, text):
        graph = deepcopy(self.graph)
        graph["tts"]["inputs"]["text"] = text
        return graph


class Audio:
    def __init__(self):
        self.converted = 0
        self.cleaned = 0
        self.error = None
    async def convert(self, source, destination, response_format, deadline):
        self.converted += 1
        if self.error:
            raise self.error
        destination.write_bytes(b"converted")
        async def cleanup():
            self.cleaned += 1
        return AudioResult(destination, "audio/mpeg", cleanup_callback=cleanup,
                           _owned_paths=(source, destination), _owned_dir=destination.parent)


class Client:
    audio_descriptor = staticmethod(ComfyClient.audio_descriptor)
    def __init__(self):
        self.posts = []
        self.deletes = []
        self.state = "running"
        self.online = True
        self.uncertain = False
        self.correlated = None
        self.observations = 0
        self.downloads = 0
        self.on_submit = None
        self.on_observe = None
        self.history = {"outputs": {"save": {"audio": [{"filename": "x.wav", "subfolder": "", "type": "output"}]}}, "status": {"status_str": "success", "completed": True}}
    async def schemas(self, classes):
        if not self.online:
            raise ComfyError()
        return {}
    async def submit(self, graph, task_id, instance_id, request_id):
        self.posts.append((graph, task_id, instance_id, request_id))
        if self.on_submit:
            await self.on_submit(task_id)
        if self.uncertain:
            raise SubmissionUncertain()
    async def observe(self, task_id):
        self.observations += 1
        if self.on_observe:
            return await self.on_observe(task_id)
        if not self.online:
            raise ComfyError()
        return Observation(self.state, self.history if self.state in {"success", "error"} else None)
    async def correlate(self, instance_id, request_id):
        return self.correlated
    async def delete_pending(self, task_id):
        self.deletes.append(task_id)
    async def download(self, descriptor, destination, deadline):
        self.downloads += 1
        destination.write_bytes(b"source")
    async def close(self):
        pass


def settings(**limits):
    return AppConfig(server=ServerConfig(api_key="test-key"), comfyui=ComfyConfig(base_url="http://mock.invalid"),
                     limits=LimitsConfig(poll_interval=0.01, probe_interval=0.05, **limits),
                     models=[ModelConfig(id="model")])


def request(text="private input", voice="voice"):
    return SpeechRequest(model="model", voice=voice, input=text)


def make_service(tmp_path, monkeypatch, *, client=None, config=None, **kwargs):
    monkeypatch.setattr("comfybridge.service.validate_live", lambda preset, schemas: ["schema_invalid"] if preset.preset.voice == "bad" else [])
    client = client or Client()
    service = BridgeService(config or settings(), tmp_path, client=client, audio_processor=kwargs.pop("audio_processor", Audio()),
                            presets=kwargs.pop("presets", {("model", "voice"): Preset()}), **kwargs)
    return service, client


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_success_private_copy_journal_and_delivery_capacity(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(max_queue=0))
    client.state = "success"
    await service.start()
    try:
        result = await service.synthesize(request("  exact\n😀 "), str(uuid.uuid4()))
        assert client.posts[0][0]["tts"]["inputs"]["text"] == "  exact\n😀 "
        assert service.presets[("model", "voice")].graph["tts"]["inputs"]["text"] == "template"
        assert service.status()["delivery"] == 1
        assert service.status()["slots"] == 1
        assert not service.is_drained()
        with pytest.raises(BridgeError) as full:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert full.value.status == 429
        assert "exact" not in str(service.status())
        task_id = client.posts[0][1]
        assert service.journal.get(task_id)["state"] == "success"
        await result.cleanup()
        await result.cleanup()
        assert service.audio.cleaned == 1
        assert service.status()["slots"] == 0
        assert service.is_drained()
        assert not list((tmp_path / "tmp").glob("cb-*"))
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_intent_committed_before_post(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch)
    async def check(task_id):
        other = Journal(tmp_path / "runtime" / "tasks.sqlite3")
        assert other.get(task_id)["state"] == "intent"
        other.close()
    client.on_submit = check
    client.state = "success"
    await service.start()
    try:
        result = await service.synthesize(request(), str(uuid.uuid4()))
        await result.cleanup()
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_deadline_running_does_not_release_remote_slot(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.06, max_queue=0))
    await service.start()
    try:
        with pytest.raises(BridgeError) as error:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert error.value.status == 504
        assert error.value.submitted
        assert service.status()["slots"] == 1
        assert not service.is_drained()
        with pytest.raises(BridgeError) as full:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert full.value.status == 429
        assert len(client.posts) == 1 and not client.deletes
        client.state = "success"
        await eventually(service.is_drained)
        assert client.downloads == 0
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_waiting_deadline_never_submits_and_fifo_capacity(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.12, max_queue=1))
    await service.start()
    first = asyncio.create_task(service.synthesize(request("first"), str(uuid.uuid4())))
    await eventually(lambda: len(client.posts) == 1)
    second = asyncio.create_task(service.synthesize(request("second"), str(uuid.uuid4())))
    await eventually(lambda: service.status()["waiting"] == 1)
    try:
        with pytest.raises(BridgeError) as full:
            await service.synthesize(request("third"), str(uuid.uuid4()))
        assert full.value.status == 429
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, BridgeError) and result.status == 504 for result in results)
        assert len(client.posts) == 1
        client.state = "success"
        await eventually(service.is_drained)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_pending_delete_requires_two_absences_and_200_not_proof(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.05))
    client.state = "pending"
    await service.start()
    try:
        with pytest.raises(BridgeError):
            await service.synthesize(request(), str(uuid.uuid4()))
        await eventually(lambda: bool(client.deletes))
        assert service.status()["slots"] == 1
        task_id = client.posts[0][1]
        assert client.deletes and all(deleted == task_id for deleted in client.deletes)
        first_absence = asyncio.Event()
        allow_second = asyncio.Event()
        observations = 0
        async def absent(_):
            nonlocal observations
            observations += 1
            if observations == 1:
                first_absence.set()
            else:
                await allow_second.wait()
            return Observation("absent")
        client.on_observe = absent
        await first_absence.wait()
        await asyncio.sleep(0)
        assert service.journal.get(task_id)["absent_count"] == 1
        assert service.status()["slots"] == 1
        allow_second.set()
        await eventually(service.is_drained)
        assert service.journal.get(task_id)["state"] == "not_active"
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_pending_delete_start_race_lets_running_finish(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.04))
    client.state = "pending"
    await service.start()
    try:
        with pytest.raises(BridgeError):
            await service.synthesize(request(), str(uuid.uuid4()))
        await eventually(lambda: bool(client.deletes))
        client.state = "running"
        await eventually(lambda: service.journal.get(client.posts[0][1])["state"] == "running")
        assert service.status()["slots"] == 1
        assert len(client.deletes) == 1
        client.state = "error"
        await eventually(service.is_drained)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_lost_response_unknown_blocks_new_requests_and_restart_never_replays(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.06))
    client.uncertain = True
    client.state = "absent"
    await service.start()
    with pytest.raises(BridgeError) as uncertain:
        await service.synthesize(request(), str(uuid.uuid4()))
    assert uncertain.value.code == "deadline_exceeded"
    task_id = client.posts[0][1]
    with pytest.raises(BridgeError) as blocked:
        await service.synthesize(request(), str(uuid.uuid4()))
    assert blocked.value.status == 503
    assert len(client.posts) == 1
    await service.close(force=True)
    recovered, new_client = make_service(tmp_path, monkeypatch)
    new_client.state = "absent"
    await recovered.start()
    try:
        await eventually(lambda: new_client.observations > 1)
        assert not new_client.posts and not new_client.deletes
        assert recovered.status()["slots"] == 1
        assert not recovered.status()["ready"]
        await recovered.resolve_uncertain(task_id)
        await eventually(recovered.is_drained)
        assert recovered.status()["ready"]
    finally:
        await recovered.close(force=True)


@pytest.mark.asyncio
async def test_recovered_task_uses_original_backend_and_never_downloads(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "runtime" / "tasks.sqlite3")
    task_id = str(uuid.uuid4())
    journal.intent(task_id=task_id, request_id=str(uuid.uuid4()), backend_url="http://old.invalid", template_version="a" * 64, config_version="b" * 64)
    journal.update(task_id, state="running", admitted=1)
    journal.close()
    old = Client()
    old.state = "success"
    requested_urls = []
    def factory(url):
        requested_urls.append(url)
        return old
    service, client = make_service(tmp_path, monkeypatch, client_factory=factory)
    await service.start()
    try:
        await eventually(service.is_drained)
        assert requested_urls == ["http://old.invalid"]
        assert not client.posts and not old.posts
        assert old.downloads == 0
        assert service.journal.get(task_id)["state"] == "success"
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_per_preset_readiness_and_offline_models_independent(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, presets={
        ("model", "voice"): Preset(), ("model", "bad"): Preset("bad")})
    client.state = "success"
    await service.start()
    try:
        assert service.status()["ready"]
        with pytest.raises(BridgeError) as unready:
            await service.synthesize(request(voice="bad"), str(uuid.uuid4()))
        assert unready.value.code == "preset_unready"
        result = await service.synthesize(request(), str(uuid.uuid4()))
        await result.cleanup()
        client.online = False
        await service._probe()
        with pytest.raises(BridgeError) as offline:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert offline.value.status == 503
        assert len(client.posts) == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_media_failure_cleans_owned_files_and_releases(tmp_path, monkeypatch):
    audio = Audio()
    audio.error = AudioError("audio_tool_failed", "sensitive raw output")
    service, client = make_service(tmp_path, monkeypatch, audio_processor=audio)
    client.state = "success"
    await service.start()
    try:
        with pytest.raises(BridgeError) as error:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert str(error.value) == "audio_tool_failed"
        await eventually(service.is_drained)
        assert not list((tmp_path / "tmp").glob("cb-*"))
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_drain_allows_existing_waiter_and_rejects_new(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=2.0))
    await service.start()
    first = asyncio.create_task(service.synthesize(request("first"), str(uuid.uuid4())))
    await eventually(lambda: len(client.posts) == 1)
    second = asyncio.create_task(service.synthesize(request("second"), str(uuid.uuid4())))
    await eventually(lambda: service.status()["waiting"] == 1)
    service.begin_drain()
    try:
        with pytest.raises(BridgeError) as draining:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert draining.value.code == "service_draining"
        client.state = "success"
        first_result = await first
        assert len(client.posts) == 1
        await first_result.cleanup()
        second_result = await second
        assert [post[0]["tts"]["inputs"]["text"] for post in client.posts] == ["first", "second"]
        await second_result.cleanup()
        await eventually(service.is_drained)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_uncertain_post_recovers_and_delivers_to_original_caller(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=1.0))
    client.uncertain = True
    client.state = "absent"
    await service.start()
    call = asyncio.create_task(service.synthesize(request(), str(uuid.uuid4())))
    try:
        await eventually(lambda: client.observations > 1)
        assert not call.done()
        with pytest.raises(BridgeError) as blocked:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert blocked.value.status == 503
        client.state = "success"
        result = await call
        assert len(client.posts) == 1
        assert client.downloads == 1
        await result.cleanup()
        await eventually(service.is_drained)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_native_rejection_releases_capacity(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch)
    async def reject(task_id):
        raise SubmissionRejected()
    client.on_submit = reject
    await service.start()
    try:
        with pytest.raises(BridgeError, match="submission_rejected") as error:
            await service.synthesize(request(), str(uuid.uuid4()))
        assert error.value.status == 502
        await eventually(service.is_drained)
        assert service.journal.get(client.posts[0][1])["state"] == "error"
        assert not client.deletes
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_submit_attempt_bounded_by_request_budget(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.04))
    async def slow(task_id):
        await asyncio.sleep(5)
    client.on_submit = slow
    client.state = "absent"
    await service.start()
    try:
        with pytest.raises(BridgeError, match="deadline_exceeded"):
            await service.synthesize(request(), str(uuid.uuid4()))
        await eventually(lambda: service.journal.get(client.posts[0][1])["state"] == "uncertain")
        assert service.status()["slots"] == 1
        assert len(client.posts) == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_startup_janitor_removes_only_recorded_owned_files(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "runtime" / "tasks.sqlite3")
    task_id = str(uuid.uuid4())
    journal.intent(task_id=task_id, request_id=str(uuid.uuid4()), backend_url="http://mock.invalid", template_version="a" * 64, config_version="b" * 64)
    journal.update(task_id, state="success")
    journal.close()
    owned = tmp_path / "tmp" / ("cb-" + task_id)
    owned.mkdir(parents=True)
    (owned / "source.audio").write_bytes(b"private")
    (owned / "output.wav").write_bytes(b"private")
    unknown = tmp_path / "tmp" / ("cb-" + str(uuid.uuid4()))
    unknown.mkdir()
    (unknown / "source.audio").write_bytes(b"unrelated")
    service, client = make_service(tmp_path, monkeypatch)
    await service.start()
    try:
        assert not owned.exists()
        assert (unknown / "source.audio").read_bytes() == b"unrelated"
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_acknowledgment_is_not_reopened_by_in_progress_observation(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.04))
    client.uncertain = True
    client.state = "absent"
    await service.start()
    try:
        with pytest.raises(BridgeError):
            await service.synthesize(request(), str(uuid.uuid4()))
        task_id = client.posts[0][1]
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed(task_id):
            entered.set()
            await finish.wait()
            return Observation("pending")
        client.on_observe = delayed
        await entered.wait()
        await service.resolve_uncertain(task_id)
        finish.set()
        await eventually(service.is_drained)
        assert service.journal.get(task_id)["state"] == "resolved"
        assert not client.deletes
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_reserved_worker_does_not_post_after_other_becomes_uncertain(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(max_inflight=2, request_timeout=0.08))
    client.uncertain = True
    client.state = "absent"
    await service.start()
    try:
        first = asyncio.create_task(service.synthesize(request("first"), str(uuid.uuid4())))
        second = asyncio.create_task(service.synthesize(request("second"), str(uuid.uuid4())))
        errors = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(error, BridgeError) for error in errors)
        assert len(client.posts) == 1
        assert client.posts[0][0]["tts"]["inputs"]["text"] == "first"
        assert service.status()["slots"] == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_disconnect_probe_failure_never_orphans_delivery(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch)
    async def disconnected():
        if client.posts:
            raise OSError("sensitive upstream failure")
        return False
    await service.start()
    try:
        with pytest.raises(BridgeError, match="request_tracking_failed"):
            await service.synthesize(request(), str(uuid.uuid4()), disconnected=disconnected)
        task_id = client.posts[0][1]
        assert service.journal.get(task_id)["client_state"] == "abandoned"
        assert service.status()["slots"] == 1
        client.state = "success"
        await eventually(service.is_drained)
        assert client.downloads == 0
        assert service.status()["delivery"] == 0
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_periodic_retention_protects_live_delivery(tmp_path, monkeypatch):
    import time
    service, client = make_service(tmp_path, monkeypatch)
    client.state = "success"
    await service.start()
    try:
        result = await service.synthesize(request(), str(uuid.uuid4()))
        task_id = client.posts[0][1]
        with service.journal.db:
            service.journal.db.execute("UPDATE tasks SET updated=? WHERE task_id=?", (time.time() - 8 * 86400, task_id))
        await asyncio.sleep(0.08)
        assert service.journal.get(task_id)["state"] == "success"
        await result.cleanup()
        with service.journal.db:
            service.journal.db.execute("UPDATE tasks SET updated=? WHERE task_id=?", (time.time() - 8 * 86400, task_id))
        await eventually(lambda: task_id not in service.journal.known_task_ids())
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_failed_audio_send_journals_abandoned_and_releases(tmp_path, monkeypatch):
    import time
    from comfybridge.api import _OwnedAudioResponse
    service, client = make_service(tmp_path, monkeypatch)
    client.state = "success"
    await service.start()
    try:
        result = await service.synthesize(request(), str(uuid.uuid4()))
        response = _OwnedAudioResponse(result, request_id="test", deadline=time.monotonic() + 1)
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            raise OSError("broken socket")
        with pytest.raises(OSError):
            await response({"type": "http", "method": "POST", "headers": []}, receive, send)
        assert service.journal.get(client.posts[0][1])["client_state"] == "abandoned"
        await eventually(service.is_drained)
        assert service.audio.cleaned == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_expired_waiters_do_not_accumulate_behind_stuck_remote(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.03, max_queue=1))
    await service.start()
    try:
        with pytest.raises(BridgeError):
            await service.synthesize(request("active"), str(uuid.uuid4()))
        for index in range(6):
            with pytest.raises(BridgeError, match="deadline_exceeded"):
                await service.synthesize(request(str(index)), str(uuid.uuid4()))
            assert len(service._queue) == 0
        assert len(client.posts) == 1
        assert service.status()["slots"] == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_hanging_disconnect_probe_obeys_total_deadline(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch, config=settings(request_timeout=0.03))
    async def disconnected():
        await asyncio.sleep(10)
        return False
    await service.start()
    try:
        async with asyncio.timeout(0.5):
            with pytest.raises(BridgeError, match="deadline_exceeded"):
                await service.synthesize(request(), str(uuid.uuid4()), disconnected=disconnected)
        assert service.status()["slots"] == 1
        client.state = "success"
        await eventually(service.is_drained)
        assert client.downloads == 0
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_disconnect_racing_with_result_cleans_instead_of_returning(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch)
    client.state = "success"
    async def disconnected():
        await eventually(lambda: service.status()["delivery"] == 1)
        return True
    await service.start()
    try:
        with pytest.raises(BridgeError, match="client_disconnected"):
            await service.synthesize(request(), str(uuid.uuid4()), disconnected=disconnected)
        await eventually(service.is_drained)
        assert service.audio.cleaned == 1
        assert not service._delivered_results
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_cancellation_during_submit_does_not_cancel_post_or_release(tmp_path, monkeypatch):
    service, client = make_service(tmp_path, monkeypatch)
    entered = asyncio.Event()
    finish = asyncio.Event()
    async def submit(task_id):
        entered.set()
        await finish.wait()
    client.on_submit = submit
    await service.start()
    call = asyncio.create_task(service.synthesize(request(), str(uuid.uuid4())))
    await entered.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    task_id = client.posts[0][1]
    try:
        assert service.status()["slots"] == 1
        with pytest.raises(BridgeError, match="submission_in_progress"):
            await service.resolve_uncertain(task_id)
        finish.set()
        await eventually(lambda: service.journal.get(task_id)["state"] == "running")
        assert len(client.posts) == 1
        client.state = "success"
        await eventually(service.is_drained)
        assert client.downloads == 0
    finally:
        await service.close(force=True)
