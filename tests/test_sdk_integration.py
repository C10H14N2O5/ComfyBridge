"""SDK-to-ASGI-to-mocked-Comfy integration. No live GPU/network requests."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import shutil
import struct
import uuid
import wave

import httpx
from openai import AsyncOpenAI, BadRequestError
import pytest

from comfybridge.api import create_app
from comfybridge.comfy_client import ComfyClient
from comfybridge.config import AppConfig, PresetConfig
from comfybridge.service import BridgeService
from comfybridge.workflows import import_template


def wave_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(22050)
        f.writeframes(b''.join(struct.pack('<h', int(1000 * math.sin(i / 22050 * 440 * math.tau))) for i in range(4410)))
    return output.getvalue()


class FakeComfy:
    def __init__(self):
        self.posts = []
        self.history = {}
        self.audio = wave_bytes()

    def handle(self, request):
        path = request.url.path
        if path.startswith('/object_info/'):
            kind = path.rsplit('/', 1)[1]
            schema = {
                'TestSynth': {'input': {'required': {'text': ['STRING', {}]}}, 'output': ['AUDIO'], 'output_node': False},
                'SaveAudio': {'input': {'required': {'audio': ['AUDIO', {}], 'filename_prefix': ['STRING', {}]}},
                              'output': ['AUDIO'], 'output_node': True},
            }[kind]
            return httpx.Response(200, json={kind: schema})
        if path == '/queue':
            return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
        if path == '/prompt':
            body = json.loads(request.content)
            self.posts.append(body)
            ident = body['prompt_id']
            self.history[ident] = {'status': {'status_str': 'success', 'completed': True},
                                   'prompt': [0, ident, body['prompt'], body['extra_data'], ['4']],
                                   'outputs': {'4': {'audio': [{'filename': 'result.wav', 'subfolder': 'test', 'type': 'output'}]}}}
            return httpx.Response(200, json={'prompt_id': ident, 'number': 0, 'node_errors': {}})
        if path.startswith('/history/'):
            ident = path.rsplit('/', 1)[1]
            return httpx.Response(200, json={ident: self.history[ident]} if ident in self.history else {})
        if path == '/history':
            return httpx.Response(200, json=self.history)
        if path == '/view':
            return httpx.Response(200, content=self.audio, headers={'content-type': 'audio/wav'})
        raise AssertionError(f'Unexpected fake route {request.method} {path}')


def make_config(tmp_path):
    graph = {
        '2': {'class_type': 'TestSynth', 'inputs': {'text': 'placeholder'}},
        '4': {'class_type': 'SaveAudio', 'inputs': {'audio': ['2', 0], 'filename_prefix': 'test'}},
    }
    ident = str(uuid.uuid4())
    relative, digest = import_template(tmp_path, graph, 'index-tts-2.5', 'example', ident)
    return AppConfig.model_validate({
        'server': {'api_key': 'integration-test-only'},
        'comfyui': {'base_url': 'http://comfy.invalid:8188'},
        'limits': {'poll_interval': 0.01, 'probe_interval': 0.05, 'request_timeout': 10.0},
        'models': [{'id': 'index-tts-2.5'}],
        'presets': [{'id': ident, 'model': 'index-tts-2.5', 'voice': 'example',
                     'template': relative, 'sha256': digest, 'text_binding': {'node_id': '2', 'field': 'text'},
                     'audio_output_node': '4'}],
    })


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg is required')
async def test_openai_sdk_binary_streaming_and_strict_errors(tmp_path):
    config = make_config(tmp_path)
    fake = FakeComfy()
    comfy = ComfyClient(config.comfyui.base_url, config.limits, transport=httpx.MockTransport(fake.handle))
    service = BridgeService(config, tmp_path, client=comfy)
    await service.start()
    app = create_app(config, tmp_path, service=service)
    sdk = AsyncOpenAI(api_key=config.server.api_key, base_url='http://bridge.test/v1', max_retries=0,
                      http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))
    try:
        models = await sdk.models.list()
        assert [model.id for model in models.data] == ['index-tts-2.5']
        for fmt, prefix in [('wav', b'RIFF'), ('mp3', b'ID3')]:
            async with sdk.audio.speech.with_streaming_response.create(
                model='index-tts-2.5', voice='example', input='测试原文 should never enter the journal.', response_format=fmt,
            ) as response:
                data = b''.join([chunk async for chunk in response.iter_bytes()])
                assert data.startswith(prefix)
                assert response.headers['cache-control'] == 'no-store'
        assert len(fake.posts) == 2
        assert fake.posts[0]['prompt']['2']['inputs']['text'] == '测试原文 should never enter the journal.'
        with pytest.raises(BadRequestError):
            await sdk.audio.speech.create(model='index-tts-2.5', voice='example', input='invalid speed', speed=1.2)
        assert len(fake.posts) == 2
        assert service.is_drained()
    finally:
        await sdk.close()
        await service.close(force=True)
    db = (tmp_path / 'runtime' / 'tasks.sqlite3').read_bytes()
    assert '测试原文'.encode() not in db
    assert b'integration-test-only' not in db
    for file in (tmp_path / 'temp').glob('**/*'):
        assert not file.is_file(), f'Unexpected temporary artifact: {file.name}'


async def test_offline_models_remain_available_and_speech_is_not_queued(tmp_path):
    config = make_config(tmp_path)
    def offline(request):
        raise httpx.ConnectError('private remote diagnostics', request=request)
    comfy = ComfyClient(config.comfyui.base_url, config.limits, transport=httpx.MockTransport(offline))
    service = BridgeService(config, tmp_path, client=comfy)
    await service.start()
    app = create_app(config, tmp_path, service=service)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer integration-test-only'}) as client:
            assert (await client.get('/v1/models')).status_code == 200
            result = await client.post('/v1/audio/speech', json={'model': 'index-tts-2.5', 'voice': 'example', 'input': 'test'})
            assert result.status_code == 503
            assert 'private remote' not in result.text
            assert service.is_drained()
    finally:
        await service.close(force=True)
