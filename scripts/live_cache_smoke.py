"""Explicitly authorized, bounded live cache acceptance; never run by pytest/build.

Uses an isolated new case directory, retains durable journals, and refuses to replay a
case. No queue clears, interrupts, file deletion or canvas/installation changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import secrets
import time
import uuid

import httpx
from openai import AsyncOpenAI

from comfybridge.api import create_app
from comfybridge.comfy_client import ComfyClient
from comfybridge.config import AppConfig, save_config
from comfybridge.service import BridgeService
from comfybridge.workflows import import_template

ROOT = Path(__file__).resolve().parents[1]


class Recorder:
    def __init__(self, directory):
        self.path = directory / 'live-cache-report.json'
        self.report = {'version': '0.3.0', 'completed': False, 'stage': 'initializing',
                       'submissions': [], 'free_calls': [], 'samples': [], 'cases': {}}

    def save(self):
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def stage(self, value):
        self.report['stage'] = value
        self.save()


class RecordedClient(ComfyClient):
    def __init__(self, *args, recorder, **kwargs):
        super().__init__(*args, **kwargs)
        self.recorder = recorder

    async def submit(self, graph, prompt_id, instance_id, request_id):
        # Service has already fsynced journal intent before reaching this method.
        self.recorder.report['submissions'].append({
            'prompt_id': prompt_id, 'keep_cached': graph['3']['inputs']['keep_models_cached'],
            'seed': graph['2']['inputs'].get('seed'), 'submitted_at': time.time()})
        self.recorder.save()
        await super().submit(graph, prompt_id, instance_id, request_id)

    async def free(self):
        record = {'requested_at': time.time(), 'acknowledged': False}
        self.recorder.report['free_calls'].append(record)
        self.recorder.save()
        await super().free()
        record['acknowledged'] = True
        self.recorder.save()

    async def sample(self, label):
        stats = await self._json('GET', 'system_stats')
        self.recorder.report['samples'].append({'label': label, 'sampled_at': time.time(),
            'devices': [{key: d.get(key) for key in ('name', 'vram_total', 'vram_free', 'torch_vram_total', 'torch_vram_free')}
                        for d in stats.get('devices', [])]})
        self.recorder.save()

    async def require_idle(self):
        queue = await self.queue()
        if queue['queue_running'] or queue['queue_pending']:
            raise RuntimeError('live_remote_busy')


def make_config(directory, url, method, run_id):
    graph = json.loads((ROOT / 'examples/workflows/index-tts-2.5_example.json').read_text(encoding='utf-8'))
    # Native Windows descriptors use backslashes for nested directories; the
    # bridge deliberately rejects those. Use one existing portable output folder.
    graph['4']['inputs']['filename_prefix'] = f'ComfyBridgeExperiments/idle030_{run_id}_{method}'
    ident = str(uuid.uuid4())
    relative, digest = import_template(directory, graph, 'index-tts-2.5', 'example', ident)
    config = AppConfig.model_validate({
        'server': {'api_key': secrets.token_hex(24)}, 'comfyui': {'base_url': url},
        'limits': {'poll_interval': 0.25, 'probe_interval': 3.0, 'request_timeout': 300.0},
        'models': [{'id': 'index-tts-2.5', 'cache_policy': 'idle_release',
                    'idle_release': {'seconds': 10, 'method': method, 'text': '好。'}}],
        'presets': [{'id': ident, 'model': 'index-tts-2.5', 'voice': 'example',
                     'template': relative, 'sha256': digest,
                     'text_binding': {'node_id': '2', 'field': 'text'}, 'audio_output_node': '4',
                     'cache_binding': {'node_id': '3', 'field': 'keep_models_cached'}}],
    })
    save_config(directory, config)
    return config


async def wait_state(service, wanted, timeout=330):
    async with asyncio.timeout(timeout):
        while True:
            cache = service.status()['cache'][0]
            if cache['state'] == wanted:
                return cache
            if cache['state'] in {'failed', 'uncertain', 'stale'}:
                raise RuntimeError('live_cache_operation_not_completed')
            await asyncio.sleep(.2)


async def run_case(directory, url, method, run_id, recorder):
    directory.mkdir()
    config = make_config(directory, url, method, run_id)
    client = RecordedClient(url, config.limits, recorder=recorder)
    service = BridgeService(config, directory, client=client)
    sdk = None
    try:
        await client.require_idle()
        await service.start()
        if not service.status()['ready']:
            raise RuntimeError('live_preset_not_ready')
        sdk = AsyncOpenAI(api_key=config.server.api_key, base_url='http://bridge.test/v1', max_retries=0,
                          http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, directory, service=service)),
                                                       trust_env=False, timeout=330))
        durations = []
        async def speech(text):
            await client.require_idle()
            started = time.monotonic()
            async with sdk.audio.speech.with_streaming_response.create(
                    model='index-tts-2.5', voice='example', input=text, response_format='wav') as response:
                payload = b''.join([chunk async for chunk in response.iter_bytes()])
                assert payload.startswith(b'RIFF')
            durations.append({'seconds': round(time.monotonic() - started, 3), 'audio_bytes': len(payload)})

        recorder.stage(method + '_baseline')
        await client.sample(method + '_baseline')
        if method == 'free':
            recorder.stage('free_business')
            await speech('这是接口卸载测试。')
            await client.sample('free_after_business_keep')
            await wait_state(service, 'requested')
            await asyncio.sleep(2)
            await client.sample('free_after_acknowledgment')
            recorder.report['cases']['free'] = {'accepted': True, 'business': durations,
                'vram_release_not_guaranteed_for_custom_index_cache': True}
        else:
            recorder.stage('workflow_first_business')
            await speech('这是定时缓存验证。')
            await wait_state(service, 'countdown', timeout=5)
            await client.sample('workflow_after_first_keep')
            await asyncio.sleep(2.2)
            before = service.status()['cache'][0]['remaining_seconds']
            recorder.stage('workflow_reset_business')
            await speech('倒计时重新开始。')
            after = (await wait_state(service, 'countdown', timeout=5))['remaining_seconds']
            assert after > before
            assert len(recorder.report['submissions']) == 3  # free business + two workflow business requests
            recorder.stage('workflow_first_cleanup')
            await wait_state(service, 'completed')
            await client.sample('workflow_after_first_cleanup')
            recorder.stage('workflow_reload_business')
            await speech('卸载后再次生成。')
            await client.sample('workflow_after_reload_keep')
            recorder.stage('workflow_second_cleanup')
            await wait_state(service, 'completed')
            await client.sample('workflow_after_second_cleanup')
            assert len(recorder.report['submissions']) == 6
            cleanups = [item for item in recorder.report['submissions'] if not item['keep_cached']]
            assert len(cleanups) == 2 and cleanups[0]['seed'] != cleanups[1]['seed']
            recorder.report['cases']['workflow'] = {'cleanup_cycles': 2, 'business': durations,
                'reset_remaining_before': before, 'reset_remaining_after': after,
                'different_cleanup_seeds': True, 'reload_after_cleanup_succeeded': True}
        recorder.save()
    finally:
        # Never let teardown schedule additional work. Preserve unresolved journals on failure.
        service.begin_drain()
        if sdk is not None:
            await sdk.close()
        await service.close(force=not service.is_drained())


async def run(args):
    case_dir = args.case_dir.resolve()
    case_dir.mkdir(parents=True, exist_ok=False)  # No replay or overwriting any existing case.
    (case_dir / 'case-intent.json').write_text(json.dumps({
        'started_at': time.time(), 'authorized': True, 'max_planned_generations': 6,
        'comfyui_url': args.comfy_url, 'no_replay': True}) + '\n', encoding='utf-8')
    recorder = Recorder(case_dir)
    run_id = uuid.uuid4().hex[:12]
    try:
        # /free does not guarantee clearing IndexTTS global caches. Run workflow cases last.
        await run_case(case_dir / 'free', args.comfy_url, 'free', run_id, recorder)
        await run_case(case_dir / 'workflow', args.comfy_url, 'workflow', run_id, recorder)
        recorder.report['completed'] = True
        recorder.stage('completed')
    except BaseException as error:
        recorder.report['failure_type'] = type(error).__name__  # Never include arbitrary upstream text.
        recorder.save()
        raise
    print(json.dumps(recorder.report, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-real-generation', action='store_true', required=True)
    parser.add_argument('--case-dir', type=Path, required=True)
    parser.add_argument('--comfy-url', required=True)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
