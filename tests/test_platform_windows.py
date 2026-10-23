import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from comfybridge import platform_windows as platform


def test_detached_process_has_no_console_or_secret_args(monkeypatch):
    calls = []
    monkeypatch.setattr(platform.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    platform.spawn_detached(['serve', '--data-dir', 'test path'])
    args, kwargs = calls[0]
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['stdout'] == subprocess.DEVNULL
    assert kwargs['stderr'] == subprocess.DEVNULL
    if os.name == 'nt':
        assert kwargs['creationflags'] & subprocess.DETACHED_PROCESS
        assert kwargs['creationflags'] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs['start_new_session'] is True
    assert 'token' not in repr(args)


def test_private_write_and_directory_binding(tmp_path):
    runtime = platform.private_runtime(tmp_path)
    target = runtime / 'secret.json'
    platform.atomic_private_json(target, {'token': 'private-value'})
    assert json.loads(target.read_text()) == {'token': 'private-value'}
    assert not list(runtime.glob('.private-*'))
    assert platform.directory_id(tmp_path) != platform.directory_id(tmp_path / 'other')
    if os.name != 'nt':
        assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name != 'nt', reason='Windows DACL verification')
def test_private_runtime_dacl_only_user_and_system(tmp_path):
    import win32security
    runtime = platform.private_runtime(tmp_path)
    target = runtime / 'control.json'
    platform.atomic_private_json(target, {'token': 'private'})
    for path in (runtime, target):
        descriptor = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        sids = {win32security.ConvertSidToStringSid(dacl.GetAce(i)[2]) for i in range(dacl.GetAceCount())}
        _, _, user = platform._windows_security()
        assert sids == {user, 'S-1-5-18'}


def test_lock_excludes_other_process_and_releases(tmp_path):
    # Child only attempts our temp-data mutex; never starts a service.
    code = '''import sys
from pathlib import Path
from comfybridge.platform_windows import InstanceLock, AlreadyRunning
try:
    with InstanceLock(Path(sys.argv[1])): pass
except AlreadyRunning:
    sys.exit(7)
'''
    with platform.InstanceLock(tmp_path):
        child = subprocess.run([sys.executable, '-c', code, str(tmp_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert child.returncode == 7
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert child.returncode == 0


def test_startup_creation_is_explicit_and_mocked(tmp_path, monkeypatch):
    if os.name != 'nt':
        pytest.skip('Windows shortcut')
    monkeypatch.setenv('APPDATA', str(tmp_path / 'mock-appdata'))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    calls = []
    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        platform.startup_shortcut(tmp_path).write_bytes(b'mock shortcut')
    monkeypatch.setattr(platform.subprocess, 'run', fake_run)
    shortcut = platform.configure_startup(tmp_path, enable=True)
    assert len(calls) == 1 and shortcut.exists()
    other = shortcut.parent / 'unrelated.lnk'
    other.write_bytes(b'keep')
    platform.configure_startup(tmp_path, enable=False)
    assert not shortcut.exists() and other.exists()
    assert len(calls) == 1
