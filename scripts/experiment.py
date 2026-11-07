"""Bounded, opt-in live example experiment. Never retries POST or interrupts ComfyUI."""
from __future__ import annotations
import argparse
import json
import os
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = ''  # resolved from COMFYBRIDGE_LIVE_COMFY_URL only before live use
CASES = {
    'baseline': ('你好，这是语音桥接服务的第一次测试。', False),
    'cached-first': ('现在测试模型缓存，保持自然清晰的声音。', True),
    'cached-second': ('这是下一次缓存测试，祝你今天心情愉快。', True),
    'release': ('语音测试即将结束，感谢你的耐心等待。', False),
}


def live_comfy_url():
    value = os.environ.get('COMFYBRIDGE_LIVE_COMFY_URL', '').strip()
    if not value:
        raise SystemExit('Set COMFYBRIDGE_LIVE_COMFY_URL to the ComfyUI base URL, e.g. http://127.0.0.1:8188')
    return value.rstrip('/')


def fetch(path, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = Request(BASE + path, data=data, headers={'Content-Type': 'application/json'})
    with urlopen(req, timeout=timeout) as res:
        return json.load(res)


def prepare(source: Path):
    original = json.loads(source.read_text(encoding='utf-8'))
    links = {row[0]: row for row in original['links']}
    graph = {}
    for node in original['nodes']:
        kind = node['type']
        schema = fetch('/object_info/' + kind)[kind]
        accepted = set(schema['input'].get('required', {})) | set(schema['input'].get('optional', {}))
        values = {k: v for k, v in node['widgets_values_named'].items() if k in accepted}
        for port in node.get('inputs', []):
            if port.get('link') is not None:
                link = links[port['link']]
                values[port['name']] = [str(link[1]), link[2]]
        missing = set(schema['input'].get('required', {})) - set(values)
        if missing:
            raise RuntimeError(f'Missing inputs on {kind}: {sorted(missing)}')
        graph[str(node['id'])] = {'class_type': kind, 'inputs': values}
    graph['2']['inputs']['text'] = '你好，这是 ComfyBridge 语音测试。'
    graph['4']['inputs']['filename_prefix'] = 'ComfyBridgeExperiments/example'
    fixture = ROOT / 'examples' / 'workflows' / 'index-tts-2.5_example.json'
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if fixture.exists():
        raise RuntimeError('Fixture already exists; inspect it instead of overwriting.')
    fixture.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Prepared API fixture: {fixture}')


def run(case):
    output = ROOT / 'artifacts' / 'experiments'
    output.mkdir(parents=True, exist_ok=True)
    record_path = output / (case + '.json')
    if record_path.exists():
        raise RuntimeError('Experiment already has an intent record. Never blindly rerun; reconcile its prompt_id.')
    queue = fetch('/queue')
    if queue['queue_running'] or queue['queue_pending']:
        raise RuntimeError('ComfyUI is busy; not submitting this experiment.')
    graph = json.loads((ROOT / 'examples' / 'workflows' / 'index-tts-2.5_example.json').read_text(encoding='utf-8'))
    text, keep = CASES[case]
    graph['2']['inputs']['text'] = text
    graph['3']['inputs']['keep_models_cached'] = keep
    graph['4']['inputs']['filename_prefix'] = 'ComfyBridgeExperiments/' + case
    ident = str(uuid.uuid4())
    record = {'case': case, 'prompt_id': ident, 'state': 'submitting', 'cache': keep, 'started_at': time.time()}
    def save():
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    save()  # durable enough for this single-process experiment; production uses SQLite transactions
    start = time.monotonic()
    try:
        reply = fetch('/prompt', {'prompt': graph, 'prompt_id': ident, 'client_id': 'comfybridge-experiment', 'extra_data': {'comfybridge': {'request_id': ident, 'case': case}}}, timeout=15)
    except Exception as exc:
        record.update(state='submission_uncertain', error_type=type(exc).__name__)
        save()
        print(json.dumps(record, ensure_ascii=False))
        return 2
    if reply.get('prompt_id') != ident:
        record.update(state='id_mismatch', returned_id=reply.get('prompt_id'))
        save()
        return 2
    record['state'] = 'accepted'
    save()
    while time.monotonic() - start < 300:
        try:
            entry = fetch('/history/' + ident).get(ident)
        except Exception:
            time.sleep(1)
            continue
        if entry:
            status = entry.get('status', {}).get('status_str')
            if status not in ('success', 'error'):
                record.update(state='protocol_error')
                save()
                return 2
            extra = entry.get('prompt', [None] * 4)[3]
            record['correlation_confirmed'] = extra.get('comfybridge', {}).get('request_id') == ident
            record['elapsed_seconds'] = round(time.monotonic() - start, 3)
            record['state'] = status
            audio = entry.get('outputs', {}).get('4', {}).get('audio', [])
            record['audio'] = audio
            save()
            if status != 'success' or len(audio) != 1:
                print(json.dumps(record, ensure_ascii=False))
                return 2
            from urllib.parse import urlencode
            info = audio[0]
            path = '/view?' + urlencode({k: info[k] for k in ('filename', 'subfolder', 'type')})
            target = output / (case + '.flac')
            with urlopen(BASE + path, timeout=30) as res, target.open('wb') as dst:
                total = 0
                while block := res.read(65536):
                    total += len(block)
                    if total > 128 * 1024 * 1024:
                        raise RuntimeError('Audio exceeds experimental download limit')
                    dst.write(block)
            record['download_bytes'] = total
            record['download'] = target.name
            save()
            print(json.dumps(record, ensure_ascii=False))
            return 0
        time.sleep(1)
    record.update(state='deadline_unknown', elapsed_seconds=round(time.monotonic() - start, 3))
    save()
    print(json.dumps(record, ensure_ascii=False))
    return 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', *CASES])
    parser.add_argument('--source', type=Path, help='Read-only exported example UI JSON for prepare')
    args = parser.parse_args()
    if args.action == 'prepare':
        if args.source is None:
            parser.error('prepare requires --source; generation requires explicit human authorization')
        BASE = live_comfy_url()
        prepare(args.source)
    else:
        BASE = live_comfy_url()
        raise SystemExit(run(args.action))
