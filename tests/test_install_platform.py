"""Isolated filesystem/Windows safety tests; never stop or modify existing processes."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from comfybridge import install_platform as module
from comfybridge.platform_windows import InstanceLock


def roots(tmp_path):
    root = tmp_path / 'Application'
    root.mkdir()
    return root, root / 'data'


def test_path_parent_traversal_and_overlap_rejected(tmp_path):
    root, data = roots(tmp_path)
    module.assert_no_overlap(root, data)
    module.assert_no_overlap(root, tmp_path / 'external')
    for target in (root, root.parent, root / '_internal', root / '.comfybridge-install'):
        with pytest.raises(module.InstallPlatformError, match='install_path_unsafe'):
            module.assert_no_overlap(root, target)
    with pytest.raises(module.InstallPlatformError):
        module.assert_safe_path(root / '..' / 'elsewhere')


def test_file_hardlinks_rejected_without_deleting_other_file(tmp_path):
    target, alias = tmp_path / 'original', tmp_path / 'alias'
    target.write_bytes(b'unchanged')
    try:
        os.link(target, alias)
    except OSError:
        pytest.skip('filesystem cannot create test hardlink')
    for path in (target, alias):
        with pytest.raises(module.InstallPlatformError, match='install_path_unsafe'):
            module.assert_safe_path(path)
    assert target.read_bytes() == alias.read_bytes() == b'unchanged'


def test_symlink_and_missing_child_below_symlink_rejected(tmp_path):
    source, link = tmp_path / 'source', tmp_path / 'link'
    source.mkdir()
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    for path in (link, link / 'not-created'):
        with pytest.raises(module.InstallPlatformError):
            module.assert_safe_path(path)


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction regression')
def test_windows_junction_rejected_before_resolution(tmp_path):
    source, junction = tmp_path / 'source', tmp_path / 'junction'
    source.mkdir()
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(source)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
    if result.returncode:
        pytest.skip('junction creation unavailable')
    try:
        with pytest.raises(module.InstallPlatformError, match='install_path_unsafe'):
            module.validate_local_ntfs(junction / 'missing')
    finally:
        assert junction.parent == tmp_path and junction.is_junction()
        junction.rmdir()  # remove only this isolated link, not its target
    assert source.is_dir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows volume check')
def test_actual_fixed_ntfs_and_device_paths(tmp_path):
    assert module.validate_local_ntfs(tmp_path / 'new' / 'path') == tmp_path / 'new' / 'path'
    for path in ('\\\\server\\share\\app', '\\\\?\\C:\\app', 'C:\\CON', 'C:\\trailing. ', 'C:\\path:stream'):
        with pytest.raises(module.InstallPlatformError):
            module.validate_local_ntfs(Path(path))


def test_shared_and_exclusive_file_leases_are_nonblocking(tmp_path):
    path = tmp_path / 'lifetime.lease'
    with module.FileLease(path), module.FileLease(path):
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.FileLease(path, exclusive=True):
                pytest.fail('exclusive acquired over readers')
    with module.FileLease(path, exclusive=True):
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.FileLease(path):
                pytest.fail('reader acquired over exclusive')
    with module.FileLease(path):
        pass


def test_cross_process_shared_lease_and_exclusive_conflict(tmp_path):
    path = tmp_path / 'lifetime.lease'
    code = '''
import sys
from pathlib import Path
from comfybridge.install_platform import FileLease, InstallPlatformError
try:
    with FileLease(Path(sys.argv[1]), exclusive=sys.argv[2]=='exclusive'):
        print('ACQUIRED')
except InstallPlatformError as error:
    print(error.code)
    raise SystemExit(7)
'''
    with module.FileLease(path):
        shared = subprocess.run([sys.executable, '-c', code, str(path), 'shared'], capture_output=True, timeout=5)
        exclusive = subprocess.run([sys.executable, '-c', code, str(path), 'exclusive'], capture_output=True, timeout=5)
    assert shared.returncode == 0 and b'ACQUIRED' in shared.stdout
    assert exclusive.returncode == 7 and b'install_busy' in exclusive.stdout
    successful = subprocess.run([sys.executable, '-c', code, str(path), 'exclusive'], capture_output=True, timeout=5)
    assert successful.returncode == 0


@pytest.mark.parametrize('content', [b'', b'not-json', b'{"state":"committed"}', b'{"state":"rolled_back"}'])
def test_any_transaction_marker_blocks_launch_even_terminal_named_states(tmp_path, content):
    root, data = roots(tmp_path)
    state = root / '.comfybridge-install'
    state.mkdir()
    (state / 'transaction.json').write_bytes(content)
    with pytest.raises(module.InstallPlatformError, match='install_transaction_incomplete'):
        with module.SharedLifetimeLeases(root, data):
            pytest.fail('marker must block launch')
    assert not data.exists()  # launch guard runs before creating the selected data lease
    with module.FileLease(state / 'install.lease', exclusive=True):
        pass  # failure released the already-acquired installation lease


def test_shared_roles_with_different_data_still_block_install_update(tmp_path):
    root, data = roots(tmp_path)
    other_data = tmp_path / 'external-data'
    with module.SharedLifetimeLeases(root, data), module.SharedLifetimeLeases(root, other_data):
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.FileLease(root / '.comfybridge-install' / 'install.lease', exclusive=True):
                pytest.fail('other role must hold root lease')
    assert (data / '.comfybridge-data.lease').is_file()
    assert (other_data / '.comfybridge-data.lease').is_file()


def test_different_installations_share_data_lease(tmp_path):
    first, second, data = tmp_path / 'first', tmp_path / 'second', tmp_path / 'data'
    first.mkdir()
    second.mkdir()
    with module.SharedLifetimeLeases(first, data), module.SharedLifetimeLeases(second, data):
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.FileLease(data / '.comfybridge-data.lease', exclusive=True):
                pytest.fail('data is in use by another installation')


def test_guard_checks_only_exact_resources_and_rechecks_without_stopping(tmp_path, monkeypatch):
    root, data = roots(tmp_path)
    executable = root / 'ComfyBridge.exe'
    executable.write_bytes(b'fixture')
    calls = []
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    monkeypatch.setattr(module, 'restart_manager_users', lambda files: calls.append(('rm', tuple(files))) or [])
    monkeypatch.setattr(module, 'inspect_install_processes', lambda path, names, **kw: calls.append(('process', path, names)) or [])
    monkeypatch.setattr(module, 'check_replace_access', lambda files: calls.append(('access', tuple(files))))
    with module.SetupGuard(root, data, [executable]) as guard:
        guard.recheck()
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.SharedLifetimeLeases(root, data):
                pytest.fail('setup owns exclusive installation lease')
    assert [call[0] for call in calls] == ['rm', 'process', 'access'] * 2
    assert calls[0][1] == (executable,)
    assert not (data / 'config.json').exists()


def test_guard_busy_detection_releases_every_lease(tmp_path, monkeypatch):
    root, data = roots(tmp_path)
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    monkeypatch.setattr(module, 'restart_manager_users', lambda files: [{'pid': 123, 'session_id': 2}])
    monkeypatch.setattr(module, 'inspect_install_processes', lambda *_a, **_k: pytest.fail('occupied resource already blocks'))
    with pytest.raises(module.InstallPlatformError, match='install_busy'):
        with module.SetupGuard(root, data):
            pytest.fail('occupied resources')
    with module.SharedLifetimeLeases(root, data):
        pass


def test_existing_legacy_role_lock_is_not_inferred_stopped_from_status(tmp_path, monkeypatch):
    # A separate process owns Local\\ backend lock, even if control/version is unrecognized.
    root, data = roots(tmp_path)
    data.mkdir()
    code = '''
import sys
from pathlib import Path
from comfybridge.platform_windows import InstanceLock
with InstanceLock(Path(sys.argv[1]), 'backend'):
    print('LOCKED', flush=True)
    sys.stdin.readline()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(data)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b'LOCKED'
        monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
        monkeypatch.setattr(module, 'restart_manager_users', lambda _: [])
        monkeypatch.setattr(module, 'inspect_install_processes', lambda *_a, **_k: [])
        monkeypatch.setattr(module, 'check_replace_access', lambda _: None)
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.SetupGuard(root, data):
                pytest.fail('legacy backend Local mutex still held')
    finally:
        child.communicate(b'\n', timeout=5)  # cooperative exact owned test child, no process kill
    assert child.returncode == 0


def test_guard_rejects_resource_outside_selected_roots(tmp_path, monkeypatch):
    root, data = roots(tmp_path)
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    outside = tmp_path / 'unrelated'
    outside.write_bytes(b'keep')
    with pytest.raises(module.InstallPlatformError, match='install_path_unsafe'):
        with module.SetupGuard(root, data, [outside]):
            pass
    assert outside.read_bytes() == b'keep'


def test_probe_only_missing_roots_creates_no_files_or_directories(tmp_path, monkeypatch):
    root, data = tmp_path / 'missing-app', tmp_path / 'missing-data'
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    monkeypatch.setattr(module, 'restart_manager_users', lambda _: [])
    monkeypatch.setattr(module, 'inspect_install_processes', lambda *_a, **_kw: [])
    monkeypatch.setattr(module, 'check_replace_access', lambda _: None)
    with module.SetupGuard(root, data, probe_only=True):
        assert not root.exists() and not data.exists()
    assert list(tmp_path.iterdir()) == []


def test_probe_only_existing_lease_detects_active_role_without_new_files(tmp_path, monkeypatch):
    root, data = roots(tmp_path)
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    with module.SharedLifetimeLeases(root, data):
        before = {str(path.relative_to(root)) for path in root.rglob('*')}
        with pytest.raises(module.InstallPlatformError, match='install_busy'):
            with module.SetupGuard(root, data, probe_only=True):
                pytest.fail('active lease')
        assert {str(path.relative_to(root)) for path in root.rglob('*')} == before


def test_guard_filters_only_its_exact_lease_files_from_resource_checks(tmp_path, monkeypatch):
    root, data = roots(tmp_path)
    data.mkdir()
    config = data / 'config.json'
    config.write_bytes(b'preserve')
    own = [root / '.comfybridge-install' / 'install.lease', data / '.comfybridge-data.lease']
    seen = []
    monkeypatch.setattr(module, 'validate_local_ntfs', lambda path, **kw: module.assert_safe_path(path, **kw))
    monkeypatch.setattr(module, 'restart_manager_users', lambda files: seen.extend(files) or [])
    monkeypatch.setattr(module, 'inspect_install_processes', lambda *_a, **_kw: [])
    monkeypatch.setattr(module, 'check_replace_access', lambda _: None)
    with module.SetupGuard(root, data, [*own, config]):
        pass
    assert seen == [config]
    assert config.read_bytes() == b'preserve'


@pytest.mark.skipif(os.name != 'nt', reason='Windows verified own worker identity')
def test_trusted_setup_identity_excludes_only_verified_self_and_same_image_parent(tmp_path, monkeypatch):
    # Windows venv has a redirector parent; the worker's real image is the base
    # interpreter. A packaged Setup worker/parent instead share one exact image.
    monkeypatch.setattr(sys, 'executable', getattr(sys, '_base_executable', sys.executable))
    trusted = module.trusted_setup_pids(Path(sys.executable))
    assert os.getpid() in trusted
    assert trusted <= {os.getpid(), os.getppid()}
    assert module.trusted_setup_pids(tmp_path / 'unrelated-Setup.exe') == set()


@pytest.mark.skipif(os.name != 'nt', reason='Windows process image inspection')
def test_other_process_with_same_setup_image_is_not_silently_ignored():
    code = "import sys; print('READY',flush=True); sys.stdin.readline()"
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert child.stdout.readline().strip() == b'READY'
        processes = module.inspect_install_processes(Path(sys.executable).parent, setup_path=Path(sys.executable))
        assert os.getpid() not in {item['pid'] for item in processes}
        assert child.pid in {item['pid'] for item in processes}
    finally:
        child.communicate(b'\n', timeout=5)
    assert child.returncode == 0


@pytest.mark.skipif(os.name != 'nt', reason='Windows process permission failure seam')
def test_uninspectable_relevant_process_fails_closed_without_shutdown(monkeypatch, tmp_path):
    class Function:
        def __init__(self, fn): self.fn = fn
        def __call__(self, *args): return self.fn(*args)
    def first(handle, pointer):
        pointer._obj.th32ProcessID = 1234567
        pointer._obj.szExeFile = 'ComfyBridge.exe'
        return True
    def denied(*_):
        ctypes.set_last_error(5)
        return None
    closed = []
    kernel = SimpleNamespace(
        CreateToolhelp32Snapshot=Function(lambda *_: 1), Process32FirstW=Function(first),
        Process32NextW=Function(lambda *_: False), OpenProcess=Function(denied),
        QueryFullProcessImageNameW=Function(lambda *_: False), GetExitCodeProcess=Function(lambda *_: False),
        CloseHandle=Function(closed.append))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *_a, **_k: kernel)
    monkeypatch.setattr(module, 'trusted_setup_pids', lambda *_: set())
    with pytest.raises(module.InstallPlatformError, match='install_process_uninspectable'):
        module.inspect_install_processes(tmp_path)
    assert closed == [1]
    assert not hasattr(kernel, 'TerminateProcess')


@pytest.mark.skipif(os.name != 'nt', reason='Windows candidate exit race regression')
@pytest.mark.parametrize('open_error,exit_ok,exit_code,allowed', [
    (87, False, 259, True),   # snapshot PID has disappeared before OpenProcess
    (5, False, 259, False),  # access denied is not proof of exit
    (0, False, 259, False),  # unknown OpenProcess failure remains uncertain
    (None, True, 0, True),   # query failed, same held handle confirms termination
    (None, True, 7, True),   # nonzero exit also confirms termination
    (None, True, 259, False),  # STILL_ACTIVE is never treated as exited
    (None, False, 0, False),   # failed exit query cannot prove termination
])
def test_snapshot_candidate_exit_is_skipped_only_when_confirmed(monkeypatch, tmp_path, open_error, exit_ok, exit_code, allowed):
    class Function:
        def __init__(self, fn): self.fn = fn
        def __call__(self, *args): return self.fn(*args)
    def first(handle, pointer):
        pointer._obj.th32ProcessID = 1234567
        pointer._obj.szExeFile = 'ComfyBridge.exe'
        return True
    def following(*_):
        ctypes.set_last_error(18)
        return False
    def open_process(*_):
        if open_error is not None:
            ctypes.set_last_error(open_error)
            return None
        return 42
    exit_handles = []
    def exit_query(handle, pointer):
        exit_handles.append(handle)
        pointer._obj.value = exit_code
        return exit_ok
    closed = []
    kernel = SimpleNamespace(CreateToolhelp32Snapshot=Function(lambda *_: 1),
        Process32FirstW=Function(first), Process32NextW=Function(following),
        OpenProcess=Function(open_process), QueryFullProcessImageNameW=Function(lambda *_: False),
        GetExitCodeProcess=Function(exit_query), CloseHandle=Function(closed.append))
    monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *_a, **_k: kernel)
    monkeypatch.setattr(module, 'trusted_setup_pids', lambda *_: set())
    if allowed:
        assert module.inspect_install_processes(tmp_path) == []
    else:
        with pytest.raises(module.InstallPlatformError, match='install_process_uninspectable'):
            module.inspect_install_processes(tmp_path)
    assert closed == ([1] if open_error is not None else [42, 1])
    assert exit_handles == ([] if open_error is not None else [42])


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows snapshot/process exit race')
def test_native_short_lived_candidate_exits_between_snapshot_and_open(tmp_path, monkeypatch):
    from ctypes import wintypes
    executable = getattr(sys, '_base_executable', sys.executable)
    child = subprocess.Popen([executable, '-c', "import sys;print('READY',flush=True);sys.stdin.readline()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    native = ctypes.WinDLL('kernel32', use_last_error=True)
    native.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    native.OpenProcess.restype = wintypes.HANDLE
    native.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    native.QueryFullProcessImageNameW.restype = wintypes.BOOL
    observed, exited_handles = [], set()
    class Function:
        def __init__(self, fn): self.fn = fn
        def __call__(self, *args): return self.fn(*args)
    def open_process(access, inherit, pid):
        if pid == child.pid:
            child.communicate(b'\n', timeout=5)
            # Close only our already-terminated child's process handle, allowing
            # Windows to retire its PID before the real OpenProcess call.
            child._handle.Close()
        result = native.OpenProcess(access, inherit, pid)
        if pid == child.pid:
            if result:
                # Windows can retain a terminated process object after all of our
                # handles close. Confirm its real kernel exit status instead of
                # assuming every exited process immediately returns error 87.
                exit_code = wintypes.DWORD()
                assert native.GetExitCodeProcess(result, ctypes.byref(exit_code))
                observed.append(('terminated_handle', exit_code.value))
                exited_handles.add(result)
            else:
                observed.append(('gone_pid', ctypes.get_last_error()))
        return result
    def image_query(handle, *args):
        if handle in exited_handles:
            # Model the transient image-query failure only for our real, already
            # terminated child. Production GetExitCodeProcess remains the native
            # API on the same actual held process handle.
            ctypes.set_last_error(31)
            return False
        return native.QueryFullProcessImageNameW(handle, *args)
    class KernelProxy:
        OpenProcess = Function(open_process)
        QueryFullProcessImageNameW = Function(image_query)
        def __getattr__(self, name): return getattr(native, name)
    try:
        assert child.stdout.readline().strip() == b'READY'
        monkeypatch.setattr(module.ctypes, 'WinDLL', lambda *_a, **_k: KernelProxy())
        monkeypatch.setattr(module, 'trusted_setup_pids', lambda *_: set())
        assert module.inspect_install_processes(tmp_path, [Path(executable).name]) == []
        assert observed in ([('gone_pid', 87)], [('terminated_handle', 0)])
        assert child.returncode == 0
    finally:
        if child.returncode is None:
            child.communicate(b'\n', timeout=5)


def test_private_acl_applies_only_to_explicit_owned_tree(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir()
    (backup / 'nested').mkdir()
    (backup / 'nested' / 'config.json').write_text('{"private":"fixture"}')
    parent_private_before = module.verify_private(tmp_path)
    module.protect_private_tree(backup)
    assert all(module.verify_private(path) for path in (backup, backup / 'nested', backup / 'nested' / 'config.json'))
    assert module.verify_private(tmp_path) is parent_private_before  # parent/root ACL not replaced


@pytest.mark.skipif(os.name != 'nt', reason='Windows detection-only API smoke')
def test_restart_manager_exact_fixture_and_image_scan_are_read_only(tmp_path):
    path = tmp_path / 'unopened-resource.bin'
    path.write_bytes(b'keep')
    assert module.restart_manager_users([path]) == []
    assert module.inspect_install_processes(tmp_path) == []
    module.check_replace_access([path])
    assert path.read_bytes() == b'keep'


@pytest.mark.skipif(os.name != 'nt', reason='Windows executable section occupancy')
def test_replace_access_fails_on_current_python_image_without_killing_it():
    # Running image sections deny write access even when old software holds no leases.
    with pytest.raises(module.InstallPlatformError) as error:
        module.check_replace_access([Path(sys.executable)])
    assert error.value.code in {'install_busy', 'install_resource_uninspectable'}


def test_selftest_checks_actual_bundled_paths_never_path_search(tmp_path, monkeypatch):
    import importlib
    root = tmp_path / 'bundle'
    (root / 'bin').mkdir(parents=True)
    for name in ('ffmpeg', 'ffprobe'):
        (root / 'bin' / (name + ('.exe' if os.name == 'nt' else ''))).write_bytes(b'fixture')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'ComfyBridge.exe'))
    calls = []
    monkeypatch.setattr(importlib, 'import_module', lambda name: calls.append(('import', name)))
    def run(args, **kwargs):
        calls.append(('tool', args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', run)
    result = module.installation_self_test()
    assert result['ok'] is True and result['schema'] == 1
    assert result['checks'] == {'imports': True, 'ffmpeg': True, 'ffprobe': True}
    tools = [call for call in calls if call[0] == 'tool']
    assert len(tools) == 2
    assert all(Path(call[1][0]).parent == root / 'bin' and call[1][1:] == ['-version'] for call in tools)
    assert all(call[2]['stdin'] == subprocess.DEVNULL and call[2]['stderr'] == subprocess.DEVNULL for call in tools)
    assert not (root / 'data').exists()


def test_selftest_failed_import_is_sanitized_and_does_not_probe_tools(monkeypatch):
    import importlib
    def broken(_):
        raise RuntimeError('PRIVATE RAW DEPENDENCY ERROR')
    monkeypatch.setattr(importlib, 'import_module', broken)
    monkeypatch.setattr(subprocess, 'run', lambda *_a, **_k: pytest.fail('imports failed first'))
    result = module.installation_self_test()
    assert result['ok'] is False and result['error'] == 'installation_imports_failed'
    assert 'PRIVATE' not in json.dumps(result)
