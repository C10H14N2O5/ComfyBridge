"""No GPU/network: deterministic idle clocks and independently controlled remote jobs."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from comfybridge.api import SpeechRequest
from comfybridge.audio import AudioResult
from comfybridge.comfy_client import ComfyClient, ComfyError, Observation, SubmissionUncertain, SubmissionRejected
from comfybridge.config import AppConfig, Binding, ComfyConfig, IdleReleaseConfig, LimitsConfig, ModelConfig, PresetConfig, ServerConfig
from comfybridge.journal import Journal
from comfybridge.service import BridgeError, BridgeService


class Clock:
    def __init__(self, value=100.0):
        self.value = value
    def __call__(self):
        return self.value
    def wall(self):
        return 100000.0 + self.value
    def advance(self, seconds):
        self.value += seconds


class Loaded:
    def __init__(self, model, voice, idle):
        self.preset = PresetConfig(id=str(uuid.uuid5(uuid.NAMESPACE_DNS, model + '/' + voice)), model=model, voice=voice,
            template='workflows/example.json', sha256='a' * 64, text_binding=Binding(node_id='tts', field='text'),
            audio_output_node='save', cache_binding=Binding(node_id='cache', field='keep_models_cached'))
        self.idle_release = idle
        self.graph = {'tts': {'class_type': 'IndexTTS25BaseNode', 'inputs': {'text': 'template', 'seed': 7}},
                      'cache': {'class_type': 'IndexTTS25CacheControlNode', 'inputs': {'keep_models_cached': True}}}
        self.cleanup_calls = []
    def build_prompt(self, text):
        graph = deepcopy(self.graph)
        graph['tts']['inputs']['text'] = text
        return graph
    def build_cleanup_prompt(self, text, seed):
        self.cleanup_calls.append((text, seed))
        graph = self.build_prompt(text)
        graph['tts']['inputs']['seed'] = seed
        graph['cache']['inputs']['keep_models_cached'] = False
        return graph


class Audio:
    def __init__(self):
        self.calls = 0
    async def convert(self, source, destination, response_format, deadline):
        self.calls += 1
        destination.write_bytes(b'audio')
        return AudioResult(destination, 'audio/mpeg', _owned_paths=(source, destination), _owned_dir=destination.parent)


class Remote:
    audio_descriptor = staticmethod(ComfyClient.audio_descriptor)
    def __init__(self):
        self.posts, self.deletes = [], []
        self.states = {}
        self.free_calls = 0
        self.queue_calls = 0
        self.downloads = 0
        self.cleanup_state = 'success'
        self.cleanup_error = None
        self.business_error = None
        self.free_error = None
        self.queue_result = {'queue_running': [], 'queue_pending': []}
        self.queue_hook = None
        self.free_hook = None
        self.submit_hook = None
        self.online = True
        self.history = {'outputs': {'save': {'audio': [{'filename': 'x.wav', 'subfolder': '', 'type': 'output'}]}}}
    async def schemas(self, classes):
        if not self.online:
            raise ComfyError()
        return {}
    async def queue(self):
        self.queue_calls += 1
        if self.queue_hook:
            await self.queue_hook()
        if not self.online:
            raise ComfyError()
        return deepcopy(self.queue_result)
    async def free(self):
        self.free_calls += 1
        if self.free_hook:
            await self.free_hook()
        if self.free_error:
            raise self.free_error
    async def submit(self, graph, task_id, instance_id, request_id):
        cleanup = not graph['cache']['inputs']['keep_models_cached']
        self.posts.append({'graph': deepcopy(graph), 'id': task_id, 'cleanup': cleanup})
        self.states[task_id] = self.cleanup_state if cleanup else 'success'
        if self.submit_hook:
            await self.submit_hook(task_id, cleanup)
        if cleanup and self.cleanup_error:
            raise self.cleanup_error
        if not cleanup and self.business_error:
            raise self.business_error
    async def observe(self, task_id):
        if not self.online:
            raise ComfyError()
        state = self.states.get(task_id, 'absent')
        return Observation(state, self.history if state in {'success', 'error'} else None)
    async def correlate(self, instance_id, request_id):
        return None
    async def delete_pending(self, task_id):
        self.deletes.append(task_id)
        self.states[task_id] = 'absent'
    async def download(self, descriptor, destination, deadline):
        self.downloads += 1
        destination.write_bytes(b'source')
    async def close(self):
        pass


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.002)


def build(tmp_path, monkeypatch, specs=(('model', 'voice', 'free', 10),), *, remote=None, clock=None, config=None, **limits):
    clock, remote = clock or Clock(), remote or Remote()
    loaded, models = {}, {}
    for model, voice, method, seconds in specs:
        idle = IdleReleaseConfig(seconds=seconds, method=method, text='好。')
        loaded[(model, voice)] = Loaded(model, voice, idle)
        models[model] = ModelConfig(id=model, cache_policy='idle_release', idle_release=idle)
    config = config or AppConfig(server=ServerConfig(api_key='test-key'), comfyui=ComfyConfig(base_url='http://mock.invalid'),
        limits=LimitsConfig(poll_interval=0.005, probe_interval=0.02, request_timeout=limits.pop('request_timeout', 3.0), **limits),
        models=list(models.values()), presets=[entry.preset for entry in loaded.values()])
    monkeypatch.setattr('comfybridge.service.validate_live', lambda *_: [])
    service = BridgeService(config, tmp_path, presets=loaded, client=remote, audio_processor=Audio(), clock=clock, wall_clock=clock.wall)
    return service, remote, clock


async def speech(service, model='model', voice='voice', *, cleanup=True):
    result = await service.synthesize(SpeechRequest(model=model, voice=voice, input='PRIVATE BUSINESS TEXT'), str(uuid.uuid4()))
    if cleanup:
        await result.cleanup()
    return result


async def idle(service):
    await eventually(lambda: service.cache.idle_since is not None)


def advance(service, clock, amount):
    clock.advance(amount)
    service._wake.set()


def cache(service, model='model'):
    return next(row for row in service.status()['cache'] if row['model'] == model)


def cleanups(remote):
    return [post for post in remote.posts if post['cleanup']]


@pytest.mark.asyncio
async def test_never_used_models_never_release(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    await service.start()
    try:
        advance(service, clock, 10000)
        for _ in range(10):
            await asyncio.sleep(0)
        assert service.status()['cache'] == []
        assert remote.free_calls == remote.queue_calls == 0 and not remote.posts
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_full_countdown_begins_after_delivery_not_submission(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    await service.start()
    try:
        result = await speech(service, cleanup=False)
        advance(service, clock, 100)
        await asyncio.sleep(0.01)
        assert cache(service)['state'] == 'pending_idle'
        assert service.cache.idle_since is None and remote.free_calls == 0
        await result.cleanup()
        await idle(service)
        assert cache(service)['remaining_seconds'] == 10
        advance(service, clock, 9)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 0 and cache(service)['remaining_seconds'] == 1
        advance(service, clock, 1)
        await eventually(lambda: remote.free_calls == 1 and not service.cache.busy)
        assert cache(service)['state'] == 'requested'
        assert remote.downloads == service.audio.calls == 1
        advance(service, clock, 10000)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_free_coalesces_later_deadlines_but_not_workflow(tmp_path, monkeypatch):
    specs = (('a', 'v', 'free', 5), ('b', 'v', 'free', 100), ('c', 'v', 'workflow', 200))
    service, remote, clock = build(tmp_path, monkeypatch, specs)
    await service.start()
    try:
        for model in ('a', 'b', 'c'):
            await speech(service, model, 'v')
        await idle(service)
        advance(service, clock, 5)
        await eventually(lambda: remote.free_calls == 1 and not service.cache.busy)
        assert cache(service, 'a')['state'] == cache(service, 'b')['state'] == 'requested'
        assert cache(service, 'c')['state'] == 'countdown'
        assert cache(service, 'c')['remaining_seconds'] == 195
        assert not cleanups(remote)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['free', 'workflow'])
async def test_busy_remote_and_offline_postpone_without_post(tmp_path, monkeypatch, method):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', method, 2),))
    await service.start()
    try:
        await speech(service)
        await idle(service)
        remote.queue_result['queue_pending'] = [[1, 'unrelated']]
        advance(service, clock, 2)
        await eventually(lambda: cache(service)['state'] == 'waiting_remote')
        assert cache(service)['error_code'] == 'cache_remote_busy'
        assert remote.free_calls == 0 and not cleanups(remote) and not remote.deletes
        remote.queue_result['queue_pending'] = []
        remote.online = False
        advance(service, clock, 1)
        await eventually(lambda: cache(service)['error_code'] == 'cache_remote_unavailable')
        assert not cleanups(remote) and remote.free_calls == 0
        remote.online = True
        await service._probe()
        advance(service, clock, 1)
        await eventually(lambda: cache(service)['state'] in {'requested', 'completed'})
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['free', 'workflow'])
async def test_admission_during_precheck_cancels_preparation_and_restarts_full_idle(tmp_path, monkeypatch, method):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', method, 10),), max_inflight=4)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def queue_wait():
        entered.set()
        await finish.wait()
    await service.start()
    try:
        await speech(service)
        await idle(service)
        remote.queue_hook = queue_wait
        advance(service, clock, 10)
        await entered.wait()
        waiting = asyncio.create_task(speech(service))
        await eventually(lambda: service.status()['waiting'] == 1)
        assert len(remote.posts) == 1
        finish.set()
        await waiting
        remote.queue_hook = None
        await idle(service)
        assert remote.free_calls == 0 and not cleanups(remote)
        assert cache(service)['remaining_seconds'] == 10
        advance(service, clock, 10)
        await eventually(lambda: cache(service)['state'] in {'requested', 'completed'})
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_workflow_uses_last_voice_no_audio_and_no_caller_is_not_abandonment(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'one', 'workflow', 5), ('model', 'two', 'workflow', 5)))
    remote.cleanup_state = 'pending'
    await service.start()
    try:
        await speech(service, voice='one')
        await speech(service, voice='two')
        await idle(service)
        advance(service, clock, 5)
        await eventually(lambda: len(cleanups(remote)) == 1)
        task_id = cleanups(remote)[0]['id']
        await asyncio.sleep(0.02)
        assert not remote.deletes
        row = service.journal.get(task_id)
        assert row['purpose'] == 'cache_cleanup' and row['voice'] == 'two' and row['client_state'] == 'active'
        assert not service.presets[('model', 'one')].cleanup_calls
        text, seed = service.presets[('model', 'two')].cleanup_calls[0]
        assert text == '好。' and 1 <= seed <= 4294967295
        assert cache(service)['voice'] == 'two' and service.cache.busy
        remote.states[task_id] = 'success'
        await eventually(lambda: cache(service)['state'] == 'completed' and not service.cache.busy)
        assert remote.downloads == service.audio.calls == 2
        assert not (service.data_dir / 'tmp' / ('cb-' + task_id)).exists()
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_cleanup_gate_excludes_all_business_slots_and_preserves_waiter_deadline(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', 'workflow', 1),), max_inflight=4, request_timeout=0.08)
    remote.cleanup_state = 'running'
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: bool(cleanups(remote)))
        waiter = asyncio.create_task(speech(service))
        await eventually(lambda: service.status()['waiting'] == 1)
        with pytest.raises(BridgeError, match='deadline_exceeded'):
            await waiter
        assert len(remote.posts) == 2
        assert service.status()['slots'] == 1 and service.cache.busy
        advance(service, clock, 1)
        task_id = cleanups(remote)[0]['id']
        await eventually(lambda: service.journal.get(task_id)['expired'] == 1)
        assert not remote.deletes and service.cache.busy
        remote.states[task_id] = 'success'
        await eventually(lambda: not service.cache.busy)
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_cleanup_expiration_deletes_only_owned_pending_and_confirms_absence(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', 'workflow', 1),), request_timeout=2.0)
    remote.cleanup_state = 'pending'
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: bool(cleanups(remote)))
        task_id = cleanups(remote)[0]['id']
        assert not remote.deletes
        advance(service, clock, 3)
        await eventually(lambda: not service.cache.busy)
        assert remote.deletes == [task_id]
        assert service.journal.get(task_id)['state'] == 'not_active'
        assert cache(service)['state'] == 'failed'
        advance(service, clock, 100)
        await asyncio.sleep(0.01)
        assert len(cleanups(remote)) == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_uncertain_cleanup_queues_business_without_replay_until_admin_resolution(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', 'workflow', 1),), max_inflight=3)
    remote.cleanup_state = 'absent'
    remote.cleanup_error = SubmissionUncertain()
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: cache(service)['state'] == 'uncertain')
        task_id = cleanups(remote)[0]['id']
        waiting = asyncio.create_task(speech(service))
        await eventually(lambda: service.status()['waiting'] == 1)
        assert len(cleanups(remote)) == 1 and service.status()['slots'] == 1
        await service.resolve_uncertain(task_id)
        await waiting
        assert len(cleanups(remote)) == 1
        assert service.journal.get(task_id)['state'] == 'resolved'
        assert not service.cache.busy
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('code,state', [('cache_free_uncertain', 'uncertain'), ('cache_free_http_error', 'failed')])
async def test_free_failure_no_retry_no_permanent_gate_and_only_same_model_use_rearms(tmp_path, monkeypatch, code, state):
    specs = (('a', 'v', 'free', 1), ('b', 'v', 'workflow', 100))
    service, remote, clock = build(tmp_path, monkeypatch, specs)
    remote.free_error = ComfyError(code)
    await service.start()
    try:
        await speech(service, 'a', 'v')
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: cache(service, 'a')['state'] == state and not service.cache.busy)
        await speech(service, 'b', 'v')
        await idle(service)
        advance(service, clock, 20)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 1 and cache(service, 'a')['state'] == state
        remote.free_error = None
        await speech(service, 'a', 'v')
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: remote.free_calls == 2 and not service.cache.busy)
        assert cache(service, 'a')['state'] == 'requested'
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_new_arrival_after_free_post_queues_until_response(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, max_inflight=3)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def free_wait():
        entered.set()
        await finish.wait()
    remote.free_hook = free_wait
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 10)
        await entered.wait()
        waiter = asyncio.create_task(speech(service))
        await eventually(lambda: service.status()['waiting'] == 1)
        assert len(remote.posts) == 1
        finish.set()
        await waiter
        assert len(remote.posts) == 2
        await idle(service)
        assert cache(service)['state'] == 'countdown'
        assert cache(service)['remaining_seconds'] == 10
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_workflow_crash_recovery_preserves_purpose_and_deadline_without_pending_cancel(tmp_path, monkeypatch):
    specs = (('model', 'voice', 'workflow', 1),)
    service, remote, clock = build(tmp_path, monkeypatch, specs, request_timeout=30.0)
    remote.cleanup_state = 'pending'
    await service.start()
    await speech(service)
    await idle(service)
    advance(service, clock, 1)
    await eventually(lambda: bool(cleanups(remote)))
    task_id = cleanups(remote)[0]['id']
    await service.close(force=True)
    recovered, remote, clock = build(tmp_path, monkeypatch, specs, remote=remote, clock=clock, request_timeout=30.0)
    await recovered.start()
    try:
        await asyncio.sleep(0.02)
        assert len(cleanups(remote)) == 1 and not remote.deletes
        assert recovered.cache.busy
        assert recovered.journal.get(task_id)['client_state'] == 'active'
        remote.states[task_id] = 'success'
        await eventually(lambda: not recovered.cache.busy)
        assert cache(recovered)['state'] == 'completed'
        assert remote.downloads == 1
    finally:
        await recovered.close(force=True)


@pytest.mark.asyncio
async def test_restart_matching_dirty_cycle_gets_full_idle_and_mismatched_cycle_stale(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    await service.start()
    await speech(service)
    await idle(service)
    advance(service, clock, 9)
    await service.close(force=True)
    clock.advance(1000)
    recovered, remote, clock = build(tmp_path, monkeypatch, remote=remote, clock=clock)
    await recovered.start()
    try:
        await idle(recovered)
        assert cache(recovered)['remaining_seconds'] == 10 and remote.free_calls == 0
    finally:
        await recovered.close(force=True)
    stale, remote, clock = build(tmp_path, monkeypatch, remote=remote, clock=clock)
    stale.presets[('model', 'voice')].preset.sha256 = 'b' * 64
    await stale.start()
    try:
        assert cache(stale)['state'] == 'stale'
        advance(stale, clock, 1000)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 0
        await speech(stale)
        await idle(stale)
        advance(stale, clock, 10)
        await eventually(lambda: remote.free_calls == 1)
    finally:
        await stale.close(force=True)


@pytest.mark.asyncio
async def test_crashed_free_intent_is_uncertain_and_never_replayed(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    entered = asyncio.Event()
    async def hung_free():
        entered.set()
        await asyncio.Event().wait()
    remote.free_hook = hung_free
    await service.start()
    await speech(service)
    await idle(service)
    advance(service, clock, 10)
    await entered.wait()
    await service.close(force=True)
    remote.free_hook = None
    recovered, remote, clock = build(tmp_path, monkeypatch, remote=remote, clock=clock)
    await recovered.start()
    try:
        assert cache(recovered)['state'] == 'uncertain'
        assert not recovered.cache.busy and recovered.status()['slots'] == 0
        advance(recovered, clock, 1000)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 1
        await speech(recovered)
    finally:
        await recovered.close(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['free', 'workflow'])
async def test_zero_waiting_queue_business_preempts_unsubmitted_preflight(tmp_path, monkeypatch, method):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', method, 1),), max_inflight=3, max_queue=0)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def checking():
        entered.set()
        await finish.wait()
    await service.start()
    try:
        await speech(service)
        await idle(service)
        remote.queue_hook = checking
        advance(service, clock, 1)
        await entered.wait()
        assert service.cache.preflight
        waiter = asyncio.create_task(speech(service))
        await eventually(lambda: service.status()['waiting'] == 1)
        finish.set()
        await waiter
        assert remote.free_calls == 0 and not cleanups(remote)
        await idle(service)
        assert cache(service)['remaining_seconds'] == 1
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['free', 'workflow'])
@pytest.mark.parametrize('max_queue', [0, 1])
async def test_submitted_cleanup_respects_waiting_capacity_even_with_multiple_processing_slots(tmp_path, monkeypatch, method, max_queue):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', method, 1),), max_inflight=4, max_queue=max_queue)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def hold_free():
        entered.set()
        await finish.wait()
    remote.free_hook = hold_free
    remote.cleanup_state = 'running'
    waiter = None
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: service.cache.busy and not service.cache.preflight)
        if max_queue:
            waiter = asyncio.create_task(speech(service))
            await eventually(lambda: service.status()['waiting'] == 1)
        with pytest.raises(BridgeError) as error:
            await speech(service)
        assert error.value.status == 429
        assert len(remote.posts) == (2 if method == 'workflow' else 1)
        if method == 'workflow':
            remote.states[cleanups(remote)[0]['id']] = 'success'
        else:
            finish.set()
        if waiter:
            await waiter
        await eventually(lambda: not service.cache.busy)
    finally:
        await service.close(force=True)
        if waiter and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_workflow_seed_sequence_survives_restart_and_exhaustion_blocks_post(tmp_path, monkeypatch):
    specs = (('model', 'voice', 'workflow', 1),)
    service, remote, clock = build(tmp_path, monkeypatch, specs)
    await service.start()
    await speech(service)
    await idle(service)
    advance(service, clock, 1)
    await eventually(lambda: cache(service)['state'] == 'completed')
    first_seed = cleanups(remote)[0]['graph']['tts']['inputs']['seed']
    await service.close(force=True)
    service, remote, clock = build(tmp_path, monkeypatch, specs, remote=remote, clock=clock)
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: len(cleanups(remote)) == 2 and not service.cache.busy)
        second_seed = cleanups(remote)[1]['graph']['tts']['inputs']['seed']
        assert (first_seed, second_seed) == (1, 2)
        with service.journal.db:
            service.journal.db.execute("UPDATE metadata SET value='4294967295' WHERE key='cleanup_seed_sequence'")
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: cache(service)['state'] == 'failed')
        assert cache(service)['error_code'] == 'cache_seed_exhausted'
        assert len(cleanups(remote)) == 2 and not service.cache.busy
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_known_rejected_business_never_creates_cleanup_cycle(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    remote.business_error = SubmissionRejected()
    await service.start()
    try:
        with pytest.raises(BridgeError, match='submission_rejected'):
            await speech(service)
        assert service.status()['cache'] == []
        advance(service, clock, 1000)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 0 and not cleanups(remote)
        assert service.is_drained()
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_cache_status_and_database_never_persist_business_or_cleanup_text(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch, (('model', 'voice', 'workflow', 1),))
    await service.start()
    try:
        await speech(service)
        await idle(service)
        advance(service, clock, 1)
        await eventually(lambda: cache(service)['state'] == 'completed')
        record = cache(service)
        assert set(record) == {'model', 'voice', 'method', 'seconds', 'remaining_seconds', 'state', 'error_code'}
        assert 'PRIVATE BUSINESS TEXT' not in str(service.status())
        service.journal.db.execute('PRAGMA wal_checkpoint(FULL)')
        database = (tmp_path / 'runtime' / 'tasks.sqlite3').read_bytes()
        assert b'PRIVATE BUSINESS TEXT' not in database and '好。'.encode() not in database
        assert b'test-key' not in database
    finally:
        await service.close(force=True)


@pytest.mark.asyncio
async def test_drain_cancels_preparation_and_never_starts_new_cleanup(tmp_path, monkeypatch):
    service, remote, clock = build(tmp_path, monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def checking():
        entered.set()
        await finish.wait()
    await service.start()
    try:
        await speech(service)
        await idle(service)
        remote.queue_hook = checking
        advance(service, clock, 10)
        await entered.wait()
        service.begin_drain()
        finish.set()
        await eventually(service.is_drained)
        advance(service, clock, 1000)
        await asyncio.sleep(0.01)
        assert remote.free_calls == 0 and not cleanups(remote)
    finally:
        await service.close(force=True)
