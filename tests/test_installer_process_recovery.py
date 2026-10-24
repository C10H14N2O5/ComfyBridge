"""Abrupt child-process crash assurance using real local Windows filesystem guards.

Only tiny non-executable placeholder payloads are installed into pytest tempdirs.
Self-tests are stubbed in the child/fixture through ordinary Python monkeypatches;
production exposes no environment-variable fault hooks and no backend is started.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from comfybridge.installer_core import Installer
from comfybridge.installer_manifest import build_manifest
from comfybridge import install_platform

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='real Windows NTFS/ACL/lease process recovery')
CRASH_EXIT = 79

_CHILD = r'''
import json
import os
from pathlib import Path
import sys
from comfybridge.installer_core import Installer

root, archive, manifest, setup = map(Path, sys.argv[1:5])
command, mode, crash_point, crash_path = sys.argv[5:9]
crash_occurrence = int(sys.argv[9])
seen = 0

def self_test(self, executable):
    # The payload is deliberately NOT executable; no production binary runs.
    assert executable.is_file()

def checkpoint(self, point, path=''):
    global seen
    if point == crash_point and (not crash_path or path == crash_path):
        seen += 1
        if seen == crash_occurrence:
            os._exit(79)  # no finally blocks, Python cleanup or cooperative exit

Installer._self_test = self_test
Installer._checkpoint = checkpoint
installer = Installer(root, archive, manifest, setup_path=setup)
if command == 'execute':
    result = installer.execute(installer.plan(mode))
    print(json.dumps({'executed': True, 'transaction_id': result.transaction_id}))
elif command == 'recover':
    result = installer.recover()
    print(json.dumps({'recovered': result.recovered, 'outcome': result.outcome,
                      'backup': str(result.backup_path) if result.backup_path is not None else None}))
else:
    raise AssertionError('unknown isolated test command')
'''


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release(tmp_path: Path, name: str, *, version: str, changed: bool, nested: bool = False):
    directory = tmp_path / name
    (directory / '_internal').mkdir(parents=True)
    (directory / 'BUILD-INFO.json').write_text(json.dumps({'product': 'ComfyBridge', 'version': version}), encoding='utf-8')
    (directory / 'ComfyBridge.exe').write_bytes(b'NOT-AN-EXECUTABLE-' + (b'new' if changed else b'old'))
    (directory / 'README.md').write_text('new docs' if changed else 'old docs', encoding='utf-8')
    (directory / '_internal/runtime.txt').write_bytes(b'new runtime' if changed else b'old runtime')
    if nested:
        (directory / '_internal/nested/deep').mkdir(parents=True)
        (directory / '_internal/nested/deep/runtime.txt').write_bytes(b'new deeply nested runtime')
    (directory / '_internal' / ('added.txt' if changed else 'retired.txt')).write_bytes(b'managed marker')
    archive = tmp_path / (name + '.zip')
    document = build_manifest(directory, archive)
    manifest = tmp_path / (name + '.manifest.json')
    manifest.write_text(json.dumps(document), encoding='utf-8')
    hashes = {entry['path']: entry['sha256'] for entry in document['files']}
    return archive, manifest, hashes


def seed_installation(tmp_path, monkeypatch, *, same_content: bool):
    root = tmp_path / '中文 安装目录'
    root.mkdir()
    setup_path = root / 'ComfyBridge-Setup.exe'
    setup_path.write_bytes(b'owned placeholder setup')
    old_archive, old_manifest, old_hashes = release(tmp_path, 'old-payload', version='0.3.0', changed=False)
    # No platform safety mocks: local volume checks, current-user ACL protection,
    # instance leases, Windows resource inspection and atomic replacements are real.
    monkeypatch.setattr(Installer, '_self_test', lambda self, executable: None)
    initial = Installer(root, old_archive, old_manifest, setup_path=setup_path)
    initial_result = initial.execute(initial.plan('install'))
    data = root / 'data'
    (data / 'runtime').mkdir(parents=True, exist_ok=True)
    (data / 'logs').mkdir(parents=True, exist_ok=True)
    (data / 'config.json').write_bytes(b'{"api_key":"synthetic-private-key","schema_version":1}')
    (data / 'runtime/tasks.sqlite3').write_bytes(b'opaque-original-journal-content')
    (data / 'logs/comfybridge.log').write_bytes(b'original-safe-log')
    (root / 'user-notes.txt').write_bytes(b'unknown user file must survive')
    preserved = [data / 'config.json', data / 'runtime/tasks.sqlite3', data / 'logs/comfybridge.log', root / 'user-notes.txt']
    preserved_hashes = {path: digest(path) for path in preserved}
    receipt = root / '.comfybridge-install/receipt.json'
    old_receipt = json.loads(receipt.read_text(encoding='utf-8'))
    archive, manifest, new_hashes = release(tmp_path, 'new-payload', version='0.3.0' if same_content else '0.4.0', changed=not same_content)
    return dict(root=root, setup=setup_path, archive=archive, manifest=manifest,
                mode='repair' if same_content else 'update', old_hashes=old_hashes,
                new_hashes=new_hashes, preserved_hashes=preserved_hashes, old_receipt=old_receipt,
                original_backups={path.name for path in initial_result.backup_path.parent.iterdir()})


def child(case, command, *, point='', path='', occurrence=1):
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'), PYTHONIOENCODING='utf-8')
    args = [sys.executable, '-c', _CHILD, str(case['root']), str(case['archive']), str(case['manifest']),
            str(case['setup']), command, case['mode'], point, path, str(occurrence)]
    # subprocess.run owns only this exact child handle. No PID discovery/kill is
    # used; an unexpected timeout is a test failure, not a retry or live operation.
    return subprocess.run(args, cwd=case['archive'].parent, stdin=subprocess.DEVNULL,
                          capture_output=True, env=environment, timeout=40)


def assert_preserved(case):
    assert {path: digest(path) for path in case['preserved_hashes']} == case['preserved_hashes']


def recover_and_check(case, *, outcome):
    result = child(case, 'recover')  # fresh interpreter and fresh Installer instance
    assert result.returncode == 0, result.stderr.decode('utf-8', errors='replace')
    record = json.loads(result.stdout)
    assert record['recovered'] is True and record['outcome'] == outcome
    marker = case['root'] / '.comfybridge-install/transaction.json'
    assert not marker.exists()
    assert record['backup'] is not None
    backup = Path(record['backup'])
    assert backup.is_dir() and (backup / 'backup.json').is_file()
    assert {path.name for path in backup.parent.iterdir()} == case['original_backups'] | {backup.name}
    assert install_platform.verify_private(backup)
    expected = case['new_hashes'] if outcome == 'committed' else case['old_hashes']
    for relative, expected_hash in expected.items():
        assert digest(case['root'] / relative) == expected_hash, relative
    absent = set(case['old_hashes'] if outcome == 'committed' else case['new_hashes']) - set(expected)
    for relative in absent:
        assert not (case['root'] / relative).exists(), relative
    receipt = json.loads((case['root'] / '.comfybridge-install/receipt.json').read_text(encoding='utf-8'))
    if outcome == 'rolled_back':
        assert receipt == case['old_receipt']
    else:
        assert receipt['transaction_id'] != case['old_receipt']['transaction_id']
    assert_preserved(case)
    again = child(case, 'recover')
    assert again.returncode == 0, again.stderr.decode('utf-8', errors='replace')
    assert json.loads(again.stdout) == {'recovered': False, 'outcome': 'none', 'backup': None}
    assert backup.exists()  # recovery is idempotent and never clears transaction backups


@pytest.mark.parametrize('same_content', [False, True], ids=['update', 'same-content-repair'])
@pytest.mark.parametrize('point,path,outcome', [
    ('before_withdraw', '_internal/runtime.txt', 'rolled_back'),
    ('after_withdraw', '_internal/runtime.txt', 'rolled_back'),
    ('before_publish', '_internal/runtime.txt', 'rolled_back'),
    ('after_publish', '_internal/runtime.txt', 'rolled_back'),
    ('before_publish', 'ComfyBridge.exe', 'rolled_back'),
    ('after_publish', 'ComfyBridge.exe', 'rolled_back'),
    ('before_receipt', '', 'rolled_back'),
    ('after_receipt', '', 'rolled_back'),
    ('committed', '', 'committed'),
])
def test_abrupt_process_exit_recovers_atomic_installation(tmp_path, monkeypatch, same_content, point, path, outcome):
    case = seed_installation(tmp_path, monkeypatch, same_content=same_content)
    result = child(case, 'execute', point=point, path=path)
    assert result.returncode == CRASH_EXIT, result.stderr.decode('utf-8', errors='replace')
    assert (case['root'] / '.comfybridge-install/transaction.json').is_file()
    with pytest.raises(install_platform.InstallPlatformError) as blocked:
        install_platform.assert_launch_allowed(case['root'])
    assert blocked.value.code == 'install_transaction_incomplete'
    assert_preserved(case)
    recover_and_check(case, outcome=outcome)


@pytest.mark.parametrize('same_content', [False, True], ids=['update', 'same-content-repair'])
@pytest.mark.parametrize('occurrence', [1, 2], ids=['before-old-restore', 'after-old-restore'])
def test_abrupt_exit_during_rollback_can_resume_even_with_identical_content(tmp_path, monkeypatch, same_content, occurrence):
    case = seed_installation(tmp_path, monkeypatch, same_content=same_content)
    failed_update = child(case, 'execute', point='after_receipt')
    assert failed_update.returncode == CRASH_EXIT, failed_update.stderr.decode('utf-8', errors='replace')
    failed_recovery = child(case, 'recover', point='rollback_action', path='_internal/runtime.txt', occurrence=occurrence)
    assert failed_recovery.returncode == CRASH_EXIT, failed_recovery.stderr.decode('utf-8', errors='replace')
    assert (case['root'] / '.comfybridge-install/transaction.json').is_file()
    runtime = case['root'] / '_internal/runtime.txt'
    if occurrence == 1:
        assert not runtime.exists()  # new file withdrawn, old file not restored yet
    else:
        assert digest(runtime) == case['old_hashes']['_internal/runtime.txt']
    assert_preserved(case)
    recover_and_check(case, outcome='rolled_back')


def test_fresh_install_abrupt_nested_publish_recovers_then_can_install_again(tmp_path):
    root = tmp_path / '全新 安装目录'
    root.mkdir()
    setup_path = root / 'ComfyBridge-Setup.exe'
    setup_path.write_bytes(b'owned placeholder setup')
    unrelated = tmp_path / 'unrelated-private-data.bin'
    unrelated.write_bytes(b'synthetic unrelated private data')
    preserved = {setup_path: digest(setup_path), unrelated: digest(unrelated)}
    archive, manifest, expected = release(tmp_path, 'fresh-payload', version='0.4.0', changed=True, nested=True)
    case = dict(root=root, setup=setup_path, archive=archive, manifest=manifest, mode='install', preserved_hashes=preserved)
    crashed = child(case, 'execute', point='after_publish', path='_internal/nested/deep/runtime.txt')
    assert crashed.returncode == CRASH_EXIT, crashed.stderr.decode('utf-8', errors='replace')
    assert (root / '_internal/nested/deep/runtime.txt').is_file()
    assert_preserved(case)
    restored = child(case, 'recover')
    assert restored.returncode == 0, restored.stderr.decode('utf-8', errors='replace')
    record = json.loads(restored.stdout)
    assert record['recovered'] is True and record['outcome'] == 'rolled_back'
    assert not (root / '.comfybridge-install/transaction.json').exists()
    assert not (root / '.comfybridge-install/receipt.json').exists()
    assert not (root / '_internal').exists()  # no empty managed parents to block fresh retry
    assert not (root / 'ComfyBridge.exe').exists()
    assert not (root / 'BUILD-INFO.json').exists()
    backup = Path(record['backup'])
    assert backup.is_dir() and install_platform.verify_private(backup)
    assert_preserved(case)
    # Retry is another new interpreter/Installer, not reused in-memory state.
    retried = child(case, 'execute')
    assert retried.returncode == 0, retried.stderr.decode('utf-8', errors='replace')
    assert json.loads(retried.stdout)['executed'] is True
    assert not (root / '.comfybridge-install/transaction.json').exists()
    assert {relative: digest(root / relative) for relative in expected} == expected
    assert backup.is_dir()  # the failed transaction backup was not cleared by retry
    assert len(list(backup.parent.iterdir())) == 2
    assert_preserved(case)
