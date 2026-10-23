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
from comfybridge.config import save_config
from comfybridge.control import get_status, config_digest
from comfybridge.ui_preferences import UIPreferences, save_ui_preferences, load_ui_preferences


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('release', type=Path)
    args = parser.parse_args()
    release = args.release.resolve()
    executable = release / 'ComfyBridge.exe'
    if not executable.is_file():
        parser.error('Packaged executable not found')
    fake = FakeComfy()
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
        config = make_config(data_dir)
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
            assert len(fake.posts) == 2
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
