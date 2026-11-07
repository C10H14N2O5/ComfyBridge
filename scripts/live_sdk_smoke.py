"""Explicitly authorized live acceptance: two speech calls, no POST retries.

Creates isolated artifact data, starts its own Bridge, calls it through the real
OpenAI SDK over TCP, then stops only that Bridge. Never interrupts ComfyUI.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import time
import uuid

import httpx
from openai import AsyncOpenAI

from comfybridge.config import AppConfig, save_config
from comfybridge.control import get_status
from comfybridge.launcher import ensure_backend, request_stop
from comfybridge.workflows import import_template

ROOT = Path(__file__).resolve().parents[1]


def live_comfy_url():
    value = os.environ.get('COMFYBRIDGE_LIVE_COMFY_URL', '').strip()
    if not value:
        raise SystemExit('Set COMFYBRIDGE_LIVE_COMFY_URL to the ComfyUI base URL, e.g. http://127.0.0.1:8188')
    return value.rstrip('/')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def calls(base_url, key, output, report):
    async with AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=330,
                           http_client=httpx.AsyncClient(trust_env=False, timeout=330)) as client:
        models = await client.models.list()
        report['models'] = [model.id for model in models.data]
        for fmt, text in [('wav', '你好，这是通过标准语音接口生成的测试。'),
                          ('mp3', '第二项接口测试正在进行，祝你一切顺利。')]:
            case = {'format': fmt, 'state': 'request_started'}
            report['calls'].append(case)
            save_report(output, report)
            start = time.monotonic()
            async with client.audio.speech.with_streaming_response.create(
                model='index-tts-2.5', voice='example', input=text, response_format=fmt,
            ) as response:
                destination = output / ('sdk-example.' + fmt)
                with destination.open('xb') as stream:
                    async for chunk in response.iter_bytes():
                        stream.write(chunk)
                case.update(state='success', elapsed_seconds=round(time.monotonic() - start, 3),
                            bytes=destination.stat().st_size, request_id=response.headers.get('x-request-id'),
                            content_type=response.headers.get('content-type'))
                save_report(output, report)


def save_report(output, report):
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute-authorized-live-test', action='store_true')
    args = parser.parse_args()
    if not args.execute_authorized_live_test:
        parser.error('Explicit authorization flag required: this consumes GPU resources.')
    comfy_url = live_comfy_url()
    output = ROOT / 'artifacts' / 'sdk-live'
    if output.exists():
        raise RuntimeError('Acceptance artifact directory exists; reconcile instead of rerunning.')
    # Read-only preflight. Do not join an already busy server for a voluntary test.
    with httpx.Client(base_url=comfy_url, timeout=5, trust_env=False) as remote:
        queue = remote.get('/queue').raise_for_status().json()
        if queue['queue_running'] or queue['queue_pending']:
            raise RuntimeError('ComfyUI busy; no acceptance requests submitted.')
    data_dir = output / 'data'
    data_dir.mkdir(parents=True)
    graph = json.loads((ROOT / 'examples/workflows/index-tts-2.5_example.json').read_text(encoding='utf-8'))
    graph['4']['inputs']['filename_prefix'] = 'ComfyBridgeAcceptance/example'
    preset_id = str(uuid.uuid4())
    template, digest = import_template(data_dir, graph, 'index-tts-2.5', 'example', preset_id)
    config = AppConfig.model_validate({
        'server': {'listen_host': '127.0.0.1', 'listen_port': free_port(), 'api_key': 'local-acceptance-test-only'},
        'comfyui': {'base_url': comfy_url},
        'models': [{'id': 'index-tts-2.5', 'cache_policy': 'release'}],
        'presets': [{'id': preset_id, 'model': 'index-tts-2.5', 'voice': 'example', 'template': template,
                     'sha256': digest, 'text_binding': {'node_id': '2', 'field': 'text'}, 'audio_output_node': '4',
                     'cache_binding': {'node_id': '3', 'field': 'keep_models_cached'}}],
    })
    save_config(data_dir, config)
    report = {'state': 'starting', 'calls': [], 'started_at': time.time(), 'port': config.server.listen_port}
    save_report(output, report)
    started = False
    try:
        status = ensure_backend(data_dir)
        started = True
        report['initial_status'] = status
        save_report(output, report)
        asyncio.run(calls(f'http://127.0.0.1:{config.server.listen_port}/v1', config.server.api_key, output, report))
        report['state'] = 'success'
    except Exception as error:
        report.update(state='failed', error_type=type(error).__name__)
        raise
    finally:
        if started:
            report['final_status'] = get_status(data_dir)
            request_stop(data_dir)
            until = time.monotonic() + 10
            while time.monotonic() < until and get_status(data_dir) is not None:
                time.sleep(.1)
            if get_status(data_dir) is not None:
                report['bridge_force_stop_requested'] = True
                request_stop(data_dir, force=True)  # only own isolated Bridge; remote ledger retained
            report['backend_stopped'] = get_status(data_dir) is None
        save_report(output, report)
        print(json.dumps({'state': report['state'], 'calls': report['calls'], 'backend_stopped': report.get('backend_stopped')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
