"""Real SDK/ASGI/audio pipeline with protocol-fake ComfyUI; never touches a GPU."""
import asyncio
import shutil

import httpx
from openai import AsyncOpenAI
import pytest

from comfybridge.api import create_app
from comfybridge.comfy_client import ComfyClient
from comfybridge.config import AppConfig
from comfybridge.service import BridgeService
from scripts.packaged_smoke import CacheFakeComfy, cache_config


def test_live_harness_uses_single_portable_output_folder(tmp_path):
    from scripts.live_cache_smoke import make_config
    from comfybridge.workflows import load_presets
    config = make_config(tmp_path, 'http://mock.invalid', 'workflow', 'test-case')
    graph = load_presets(config, tmp_path)[('index-tts-2.5', 'example')].graph
    prefix = graph['4']['inputs']['filename_prefix']
    assert prefix == 'ComfyBridgeExperiments/idle030_test-case_workflow'
    assert prefix.count('/') == 1 and '\\' not in prefix


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg is required')
@pytest.mark.parametrize('method', ['free', 'workflow'])
async def test_sdk_to_real_service_idle_cleanup_protocol(tmp_path, method):
    config = cache_config(tmp_path)
    document = config.model_dump(mode='json')
    document['models'][0]['idle_release']['method'] = method
    config = AppConfig.model_validate(document)
    now = [0.0]
    fake = CacheFakeComfy()
    remote = ComfyClient(config.comfyui.base_url, config.limits, transport=httpx.MockTransport(fake.handle))
    service = BridgeService(config, tmp_path, client=remote, clock=lambda: now[0])
    await service.start()
    sdk = AsyncOpenAI(api_key=config.server.api_key, base_url='http://bridge.test/v1', max_retries=0,
                      http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(config, tmp_path, service=service))))
    try:
        assert service.status()['ready']
        for cycle in range(2):
            async with sdk.audio.speech.with_streaming_response.create(
                    model='index-tts-2.5', voice='example', input=f'Private business {cycle}.', response_format='wav') as response:
                payload = b''.join([chunk async for chunk in response.iter_bytes()])
                assert payload.startswith(b'RIFF')
            await until(lambda: service.status()['cache'][0]['state'] == 'countdown')
            now[0] += 1.1
            service.cache.tick()
            await until(lambda: service.status()['cache'][0]['state'] == ('requested' if method == 'free' else 'completed'))
            await until(lambda: service.is_drained())
            assert fake.downloads == cycle + 1
            # A completed internal operation cannot rearm itself even after a long idle.
            count = (len(fake.posts), fake.free_calls)
            now[0] += 10000
            service.cache.tick()
            await asyncio.sleep(.03)
            assert (len(fake.posts), fake.free_calls) == count
        business = [p for p in fake.posts if p['prompt']['3']['inputs']['keep_models_cached']]
        cleanup = [p for p in fake.posts if not p['prompt']['3']['inputs']['keep_models_cached']]
        assert len(business) == 2
        if method == 'workflow':
            assert len(cleanup) == 2 and fake.free_calls == 0
            assert all(p['prompt']['2']['inputs']['text'] == '好。' for p in cleanup)
            assert cleanup[0]['prompt']['2']['inputs']['seed'] != cleanup[1]['prompt']['2']['inputs']['seed']
        else:
            assert not cleanup and fake.free_calls == 2
        assert service.presets[('index-tts-2.5', 'example')].graph['3']['inputs']['keep_models_cached'] is False
        assert 'Private business' not in str(service.status())
    finally:
        await sdk.close()
        await service.close(force=True)
    assert b'Private business' not in (tmp_path / 'runtime/tasks.sqlite3').read_bytes()
