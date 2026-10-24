"""Exercise the packaged EXE over TCP against a local fake ComfyUI, with PATH isolated."""
from __future__ import annotations
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx
from openai import OpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_sdk_integration import FakeComfy, make_config
from comfybridge.config import AppConfig, Binding, save_config
from comfybridge.workflows import import_template
from comfybridge.control import get_status, config_digest
from comfybridge.ui_preferences import UIPreferences, save_ui_preferences, load_ui_preferences


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class CacheFakeComfy(FakeComfy):
    """Protocol-only fake: no IndexTTS installation, model or GPU is involved."""
    def __init__(self):
        super().__init__()
        self.free_calls = 0
        self.downloads = 0

    def handle(self, request):
        path = request.url.path
        schemas = {
            'IndexTTS25BaseNode': {'input': {'required': {'text': ['STRING', {}]}, 'optional': {
                'cache_control': ['DICT', {}], 'seed': ['INT', {'min': 0, 'max': 4294967295}]}},
                'output': ['AUDIO', 'INT', 'STRING'], 'output_node': False},
            'IndexTTS25CacheControlNode': {'input': {'required': {'keep_models_cached': ['BOOLEAN', {}]}},
                'output': ['DICT'], 'output_node': False},
        }
        if path.startswith('/object_info/') and path.rsplit('/', 1)[1] in schemas:
            kind = path.rsplit('/', 1)[1]
            return httpx.Response(200, json={kind: schemas[kind]})
        if path == '/free':
            assert request.method == 'POST'
            assert json.loads(request.content) == {'unload_models': True, 'free_memory': True}
            self.free_calls += 1
            return httpx.Response(200)
        if path == '/view':
            self.downloads += 1
        return super().handle(request)


def cache_config(data_dir):
    config = make_config(data_dir)
    graph = {
        '2': {'class_type': 'IndexTTS25BaseNode', 'inputs': {
            'text': 'placeholder', 'cache_control': ['3', 0], 'seed': 123}},
        '3': {'class_type': 'IndexTTS25CacheControlNode', 'inputs': {'keep_models_cached': False}},
        '4': {'class_type': 'SaveAudio', 'inputs': {'audio': ['2', 0], 'filename_prefix': 'test'}},
    }
    preset = config.presets[0]
    preset.template, preset.sha256 = import_template(data_dir, graph, preset.model, preset.voice, preset.id)
    preset.cache_binding = Binding(node_id='3', field='keep_models_cached')
    document = config.model_dump(mode='json')
    document['models'][0].update(cache_policy='idle_release', idle_release={
        'seconds': 1, 'method': 'workflow', 'text': '好。'})
    return AppConfig.model_validate(document)


def wait_for(predicate, timeout=12):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('packaged_condition_timed_out')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('release', type=Path)
    args = parser.parse_args()
    release = args.release.resolve()
    executable = release / 'ComfyBridge.exe'
    if not executable.is_file():
        parser.error('Packaged executable not found')
    fake = CacheFakeComfy()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def log_message(self, *args): pass
        def respond(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            response = fake.handle(httpx.Request(self.command, 'http://fake' + self.path, content=body))
            self.send_response(response.status_code)
            self.send_header('Content-Type', response.headers.get('content-type', 'application/json'))
            self.send_header('Content-Length', str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)
    remote = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=remote.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env['PATH'] = str(Path(env.get('SystemRoot', r'C:\Windows')) / 'System32')
    env.pop('PYTHONPATH', None)
    env.pop('VIRTUAL_ENV', None)
    report = {'release': release.name, 'path_isolated': True, 'gpu_requests': 0}
    with tempfile.TemporaryDirectory(prefix='comfybridge-packaged-中文 空格-') as temporary:
        data_dir = Path(temporary)
        config = cache_config(data_dir)
        config.server.listen_port = free_port()
        config.comfyui.base_url = f'http://127.0.0.1:{remote.server_port}'
        save_config(data_dir, config)
        command = [str(executable), '--data-dir', str(data_dir)]
        started = False
        try:
            result = subprocess.run([*command, 'start'], env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=35)
            if result.returncode:
                raise RuntimeError('packaged_start_failed: ' + result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace'))
            started = True
            status = get_status(data_dir)
            assert status is not None
            report['started'] = True
            monitor = subprocess.run([*command, 'monitor'], input=b'q\n', capture_output=True, env=env, timeout=10)
            assert monitor.returncode == 0
            assert get_status(data_dir) is not None
            report['monitor_closed_backend_alive'] = True
            instance_id = status['instance_id']
            config_hash = config_digest(config)
            menu_results = {}
            for language, title in [('zh-CN', '功能选择'), ('en-US', 'Main menu')]:
                save_ui_preferences(data_dir, UIPreferences(language=language))
                # Summary -> back -> real Monitor -> home; no readline/os.read prefetch.
                navigation = subprocess.run(command, input=b'8\n2\n\n0\n1\n0\n0\n', capture_output=True, env=env, timeout=20)
                assert navigation.returncode == 0
                text = navigation.stdout.decode('utf-8')
                assert title in text
                assert 'Choose interface language' not in text
                assert config.server.api_key not in text
                assert get_status(data_dir)['instance_id'] == instance_id
                # Open the existing configuration through Monitor and return twice.
                nested = subprocess.run(command, input=b'1\n4\nq\n0\n0\n', capture_output=True, env=env, timeout=20)
                assert nested.returncode == 0
                assert title in nested.stdout.decode('utf-8')
                assert get_status(data_dir)['instance_id'] == instance_id
                cancelled = subprocess.run(command, input=b'4\n\n\n0\n', capture_output=True, env=env, timeout=20)
                assert cancelled.returncode == 0
                assert get_status(data_dir)['instance_id'] == instance_id
                assert not (data_dir / 'runtime/restart.json').exists()
                menu_results[language] = True
            # Explicit menu language change persists without changing service config/identity.
            switched = subprocess.run(command, input=b'a\n1\n0\n', capture_output=True, env=env, timeout=20)
            assert switched.returncode == 0 and '功能选择' in switched.stdout.decode('utf-8')
            assert load_ui_preferences(data_dir).language == 'zh-CN'
            assert get_status(data_dir)['config_digest'] == config_hash
            assert get_status(data_dir)['instance_id'] == instance_id
            report['bilingual_home_and_nested_navigation'] = menu_results
            report['language_switch_no_backend_restart'] = True
            report['cancelled_restart_no_side_effect'] = True
            with OpenAI(api_key=config.server.api_key, base_url=f'http://127.0.0.1:{config.server.listen_port}/v1',
                        max_retries=0, http_client=httpx.Client(trust_env=False, timeout=20)) as sdk:
                assert [model.id for model in sdk.models.list().data] == ['index-tts-2.5']
                formats = {}
                for fmt, magic in [('wav', b'RIFF'), ('mp3', b'ID3')]:
                    with sdk.audio.speech.with_streaming_response.create(model='index-tts-2.5', voice='example',
                            input='Packaged synthetic test only.', response_format=fmt) as response:
                        payload = b''.join(response.iter_bytes())
                        assert payload.startswith(magic)
                        formats[fmt] = len(payload)
                report['audio_bytes'] = formats
            def cache_state():
                state = get_status(data_dir)
                return state.get('cache', [{}])[0].get('state') if state else None
            wait_for(lambda: len(fake.posts) == 3 and cache_state() == 'completed')
            assert all(post['prompt']['3']['inputs']['keep_models_cached'] is True for post in fake.posts[:2])
            first_cleanup = fake.posts[2]['prompt']
            assert first_cleanup['3']['inputs']['keep_models_cached'] is False
            assert first_cleanup['2']['inputs']['text'] == '好。'
            assert fake.downloads == 2
            assert fake.free_calls == 0
            time.sleep(1.2)
            assert len(fake.posts) == 3  # Internal cleanup must not arm another timer.
            with OpenAI(api_key=config.server.api_key, base_url=f'http://127.0.0.1:{config.server.listen_port}/v1',
                        max_retries=0, http_client=httpx.Client(trust_env=False, timeout=20)) as sdk:
                response = sdk.audio.speech.create(model='index-tts-2.5', voice='example',
                        input='Second timed cycle.', response_format='wav')
                assert response.content.startswith(b'RIFF')
            wait_for(lambda: len(fake.posts) == 5 and cache_state() == 'completed')
            second_cleanup = fake.posts[4]['prompt']
            assert second_cleanup['2']['inputs']['seed'] != first_cleanup['2']['inputs']['seed']
            assert second_cleanup['3']['inputs']['keep_models_cached'] is False
            assert fake.downloads == 3
            report['workflow_idle_release'] = {'cycles': 2, 'business_requests': 3,
                'cleanup_requests': 2, 'distinct_cleanup_seeds': True, 'cleanup_audio_downloads': 0}
            # Apply /free through a deliberate stop/save/start, never hot reload.
            stopped = subprocess.run([*command, 'stop', '--yes'], env=env, stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=10)
            assert stopped.returncode == 0
            wait_for(lambda: get_status(data_dir) is None)
            document = config.model_dump(mode='json')
            document['models'][0]['idle_release']['method'] = 'free'
            config = AppConfig.model_validate(document)
            save_config(data_dir, config)
            restarted = subprocess.run([*command, 'start'], env=env, stdin=subprocess.DEVNULL,
                                       capture_output=True, timeout=35)
            assert restarted.returncode == 0
            with OpenAI(api_key=config.server.api_key, base_url=f'http://127.0.0.1:{config.server.listen_port}/v1',
                        max_retries=0, http_client=httpx.Client(trust_env=False, timeout=20)) as sdk:
                response = sdk.audio.speech.create(model='index-tts-2.5', voice='example',
                        input='Native free test.', response_format='wav')
                assert response.content.startswith(b'RIFF')
            wait_for(lambda: fake.free_calls == 1 and cache_state() == 'requested')
            time.sleep(1.2)
            assert len(fake.posts) == 6 and fake.free_calls == 1 and fake.downloads == 4
            report['native_free_idle_release'] = {'calls': 1, 'native_empty_200': True,
                'acknowledgment_not_vram_proof': True, 'unexpected_workflow_cleanup_requests': 0}
            report['fake_submissions'] = len(fake.posts)
        finally:
            if started:
                result = subprocess.run([*command, 'stop', '--yes'], env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=10)
                until = time.monotonic() + 10
                while time.monotonic() < until and get_status(data_dir) is not None:
                    time.sleep(.1)
                if get_status(data_dir) is not None:
                    subprocess.run([*command, 'stop', '--force', '--yes'], env=env,
                                   stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
                    time.sleep(.5)
                report['backend_stopped'] = get_status(data_dir) is None
                if not report['backend_stopped']:
                    raise RuntimeError('packaged_backend_did_not_stop')
            remote.shutdown()
            remote.server_close()
            thread.join(timeout=5)
    output = ROOT / 'artifacts' / 'packaged-smoke.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
