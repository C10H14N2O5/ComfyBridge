"""Installation safety boundary: detection and leases only, never process shutdown.

File sharing leases span Windows sessions; the older named data mutexes are Local
and are additional checks, not proof against arbitrary external/legacy writers.
"""
from __future__ import annotations

from contextlib import ExitStack
import ctypes
import ntpath
import os
from pathlib import Path
import re
import stat

from .platform_windows import AlreadyRunning, InstanceLock, protect_path, _windows_security


class InstallPlatformError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fail(code='install_path_unsafe'):
    raise InstallPlatformError(code)


def assert_safe_path(path: Path, *, allow_missing: bool = True) -> Path:
    """Check lexical components before resolving: no link/reparse/hardlink traversal."""
    try:
        path = Path(path)
        if any(part == '..' for part in path.parts):
            _fail()
        text = str(path)
        if any(ord(char) < 32 for char in text):
            _fail()
        if os.name == 'nt':
            if text.startswith(('\\\\', '//')):
                _fail()
            if path.drive and not re.fullmatch(r'[A-Za-z]:', path.drive):
                _fail()
            for part in path.parts[1:] if path.anchor else path.parts:
                if part.endswith((' ', '.')) or ntpath.isreserved(part):
                    _fail()
        path = Path(os.path.abspath(path))
        current = Path(path.anchor)
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    break
                _fail()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                _fail()
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                _fail()
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                _fail()
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                _fail()
        return path.resolve(strict=not allow_missing)
    except InstallPlatformError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise InstallPlatformError('install_path_unsafe') from None


def validate_local_ntfs(path: Path, *, allow_missing: bool = True) -> Path:
    path = assert_safe_path(path, allow_missing=allow_missing)
    if os.name != 'nt':
        _fail('install_volume_unsupported')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumePathNameW.restype = wintypes.BOOL
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel.GetDriveTypeW.restype = wintypes.UINT
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.restype = wintypes.BOOL
    existing = path
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    volume = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(existing), volume, len(volume)) or kernel.GetDriveTypeW(volume.value) != 3:
        _fail('install_volume_unsupported')
    filesystem = ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, filesystem, len(filesystem)):
        _fail('install_volume_unsupported')
    if filesystem.value.upper() != 'NTFS':
        _fail('install_volume_unsupported')
    return path


def assert_no_overlap(install_root: Path, data_root: Path) -> None:
    root, data = assert_safe_path(install_root), assert_safe_path(data_root)
    if root == data or root.is_relative_to(data):
        _fail()
    if data.is_relative_to(root) and not data.is_relative_to(root / 'data'):
        _fail()


def assert_launch_allowed(install_root: Path) -> None:
    root = assert_safe_path(install_root, allow_missing=False)
    state = assert_safe_path(root / '.comfybridge-install')
    marker = state / 'transaction.json'
    # lexists also blocks corrupt/broken-link markers. No transaction state is
    # interpreted here; only the transaction owner may remove its verified marker.
    if os.path.lexists(marker):
        _fail('install_transaction_incomplete')


def verify_private(path: Path) -> bool:
    try:
        path = assert_safe_path(path, allow_missing=False)
        if os.name != 'nt':
            info = path.stat()
            return not info.st_mode & 0o077 and (not hasattr(os, 'getuid') or info.st_uid == os.getuid())
        from ctypes import wintypes
        adv, kernel, user_sid = _windows_security()
        adv.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        adv.GetFileSecurityW.restype = wintypes.BOOL
        adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        adv.GetSecurityDescriptorControl.restype = wintypes.BOOL
        adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
        adv.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
        adv.GetAclInformation.restype = wintypes.BOOL
        adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        adv.GetAce.restype = wintypes.BOOL
        class ACL_SIZE_INFORMATION(ctypes.Structure):
            _fields_ = [('AceCount', wintypes.DWORD), ('AclBytesInUse', wintypes.DWORD), ('AclBytesFree', wintypes.DWORD)]
        class ACE_HEADER(ctypes.Structure):
            _fields_ = [('AceType', wintypes.BYTE), ('AceFlags', wintypes.BYTE), ('AceSize', wintypes.WORD)]
        size = wintypes.DWORD()
        adv.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
        if not size.value or size.value > 1024 * 1024:
            return False
        descriptor = ctypes.create_string_buffer(size.value)
        if not adv.GetFileSecurityW(str(path), 4, descriptor, size, ctypes.byref(size)):
            return False
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)) or not control.value & 0x1000:
            return False  # SE_DACL_PROTECTED
        present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)) or not present.value or not acl.value:
            return False
        info = ACL_SIZE_INFORMATION()
        if not adv.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2) or info.AceCount != 2:
            return False
        seen = set()
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not adv.GetAce(acl, index, ctypes.byref(ace)):
                return False
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if header.AceType != 0 or header.AceFlags & ~3 or mask != 0x1F01FF or header.AceSize < 12:
                return False
            sid = wintypes.LPWSTR()
            if not adv.ConvertSidToStringSidW(ctypes.c_void_p(ace.value + 8), ctypes.byref(sid)):
                return False
            try:
                seen.add(sid.value)
            finally:
                kernel.LocalFree(sid)
        return seen == {user_sid, 'S-1-5-18'}
    except (OSError, ValueError, RuntimeError):
        return False


def protect_private(path: Path) -> None:
    path = assert_safe_path(path, allow_missing=False)
    try:
        protect_path(path)
    except (OSError, ValueError, RuntimeError):
        raise InstallPlatformError('install_private_acl_failed') from None
    if not verify_private(path):
        _fail('install_private_acl_failed')


def protect_private_tree(path: Path) -> None:
    root = assert_safe_path(path, allow_missing=False)
    paths, pending = [], [root]
    # Validate the complete tree BEFORE ACL changes; do not follow reparse points.
    while pending:
        entry = assert_safe_path(pending.pop(), allow_missing=False)
        paths.append(entry)
        if len(paths) > 100000:
            _fail('install_tree_too_large')
        if entry.is_dir():
            try:
                pending.extend(entry.iterdir())
            except OSError:
                raise InstallPlatformError('install_private_acl_failed') from None
    for entry in paths:
        protect_private(entry)


class FileLease:
    """Shared readers versus one exclusive setup writer, nonblocking and session-wide."""
    def __init__(self, path: Path, *, exclusive: bool = False, create: bool = True):
        self.path, self.exclusive, self.create = Path(path), exclusive, create
        self.handle = None
        self.stream = None

    def __enter__(self):
        self.path = assert_safe_path(self.path, allow_missing=self.create)
        parent = assert_safe_path(self.path.parent, allow_missing=self.create)
        try:
            if self.create:
                parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise InstallPlatformError('install_lease_unavailable') from None
        assert_safe_path(parent, allow_missing=False)
        if os.name != 'nt':
            import fcntl
            self.stream = self.path.open('a+b' if self.create else 'r+b')
            try:
                if os.fstat(self.stream.fileno()).st_nlink != 1:
                    _fail()
                fcntl.flock(self.stream.fileno(), (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except (OSError, InstallPlatformError):
                self.stream.close()
                self.stream = None
                raise InstallPlatformError('install_busy') from None
            return self
        from ctypes import wintypes
        kernel = self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [('dwFileAttributes', wintypes.DWORD), ('ftCreationTime', wintypes.FILETIME),
                        ('ftLastAccessTime', wintypes.FILETIME), ('ftLastWriteTime', wintypes.FILETIME),
                        ('dwVolumeSerialNumber', wintypes.DWORD), ('nFileSizeHigh', wintypes.DWORD),
                        ('nFileSizeLow', wintypes.DWORD), ('nNumberOfLinks', wintypes.DWORD),
                        ('nFileIndexHigh', wintypes.DWORD), ('nFileIndexLow', wintypes.DWORD)]
        kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION)]
        kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        # Shared holders deny write/delete. Setup denies every share mode. Windows
        # file share checks apply across logon sessions and process lifetimes.
        access = 0x80000000 | (0x40000000 if self.exclusive else 0)
        handle = kernel.CreateFileW(str(self.path), access, 0 if self.exclusive else 1, None, 4 if self.create else 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            code = ctypes.get_last_error()
            _fail('install_busy' if code in (32, 33) else 'install_lease_unavailable')
        self.handle = handle
        try:
            info, final = BY_HANDLE_FILE_INFORMATION(), ctypes.create_unicode_buffer(32768)
            length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)) or not length or length >= len(final):
                _fail('install_lease_unavailable')
            actual = final.value[4:] if final.value.startswith('\\\\?\\') else final.value
            if (info.dwFileAttributes & (0x400 | 0x10) or info.nNumberOfLinks != 1
                    or os.path.normcase(actual) != os.path.normcase(str(self.path))):
                _fail()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class SharedLifetimeLeases:
    def __init__(self, install_root: Path, data_root: Path):
        self.root, self.data = Path(install_root), Path(data_root)
        self.stack = None

    def __enter__(self):
        self.root, self.data = assert_safe_path(self.root), assert_safe_path(self.data)
        assert_no_overlap(self.root, self.data)
        stack = self.stack = ExitStack()
        try:
            stack.enter_context(FileLease(self.root / '.comfybridge-install' / 'install.lease'))
            # Recheck marker AFTER acquiring the lifetime root lease. Setup cannot
            # publish an incomplete transaction after this succeeds.
            assert_launch_allowed(self.root)
            stack.enter_context(FileLease(self.data / '.comfybridge-data.lease'))
            return self
        except BaseException:
            stack.close()
            raise

    def __exit__(self, *exc):
        if self.stack is not None:
            self.stack.close()
            self.stack = None


# Compatibility-friendly public name for the agreed installer boundary.
LifetimeLeases = SharedLifetimeLeases


def restart_manager_users(files) -> list[dict]:
    """Register ONLY supplied existing exact file paths. Never request RM shutdown."""
    paths = []
    for file in files:
        path = assert_safe_path(file)
        if path.exists():
            if not path.is_file():
                _fail()
            paths.append(str(path))
    paths = sorted(set(paths))
    if not paths:
        return []
    if os.name != 'nt':
        _fail('install_resource_uninspectable')
    if len(paths) > 100000:
        _fail('install_tree_too_large')
    from ctypes import wintypes
    rm = ctypes.WinDLL('rstrtmgr', use_last_error=True)
    class RM_UNIQUE_PROCESS(ctypes.Structure):
        _fields_ = [('dwProcessId', wintypes.DWORD), ('ProcessStartTime', wintypes.FILETIME)]
    class RM_PROCESS_INFO(ctypes.Structure):
        _fields_ = [('Process', RM_UNIQUE_PROCESS), ('strAppName', wintypes.WCHAR * 256),
                    ('strServiceShortName', wintypes.WCHAR * 64), ('ApplicationType', wintypes.DWORD),
                    ('AppStatus', wintypes.ULONG), ('TSSessionId', wintypes.DWORD), ('bRestartable', wintypes.BOOL)]
    rm.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    rm.RmStartSession.restype = wintypes.DWORD
    rm.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR),
                                     wintypes.UINT, ctypes.POINTER(RM_UNIQUE_PROCESS), wintypes.UINT, ctypes.POINTER(wintypes.LPCWSTR)]
    rm.RmRegisterResources.restype = wintypes.DWORD
    rm.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT), ctypes.POINTER(RM_PROCESS_INFO), ctypes.POINTER(wintypes.DWORD)]
    rm.RmGetList.restype = wintypes.DWORD
    rm.RmEndSession.argtypes = [wintypes.DWORD]
    rm.RmEndSession.restype = wintypes.DWORD
    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(33)
    if rm.RmStartSession(ctypes.byref(session), 0, key) != 0:
        _fail('install_resource_uninspectable')
    try:
        for offset in range(0, len(paths), 256):
            batch = paths[offset:offset + 256]
            strings = (wintypes.LPCWSTR * len(batch))(*batch)
            if rm.RmRegisterResources(session, len(batch), strings, 0, None, 0, None) != 0:
                _fail('install_resource_uninspectable')
        needed, count, reason = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        for _ in range(4):
            capacity = needed.value
            if capacity > 10000:
                _fail('install_resource_uninspectable')
            array = (RM_PROCESS_INFO * capacity)() if capacity else None
            count.value = capacity
            result = rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), array, ctypes.byref(reason))
            if result == 0:
                return [{'pid': array[index].Process.dwProcessId, 'session_id': array[index].TSSessionId} for index in range(count.value)]
            if result != 234:  # ERROR_MORE_DATA, bounded retry for racing process lists.
                _fail('install_resource_uninspectable')
        _fail('install_resource_uninspectable')
    finally:
        rm.RmEndSession(session)


def trusted_setup_pids(setup_path: Path | None = None) -> set[int]:
    """Only this worker and its verified same-image parent may be our Setup instance."""
    import sys
    if os.name != 'nt':
        return {os.getpid()}
    try:
        expected = Path(sys.executable).resolve()
        if setup_path is not None and Path(setup_path).resolve() != expected:
            return set()
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        trusted = set()
        for pid in {os.getpid(), os.getppid()}:
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                image, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(length)) and Path(image.value).resolve() == expected:
                    trusted.add(pid)
            finally:
                kernel.CloseHandle(handle)
        return trusted if os.getpid() in trusted else set()
    except (OSError, ValueError, RuntimeError):
        return set()


def inspect_install_processes(install_root: Path, resource_names=(), *, setup_path: Path | None = None) -> list[dict]:
    """Inspect image paths. Uninspectable relevant executable names fail closed."""
    root = assert_safe_path(install_root)
    if os.name != 'nt':
        _fail('install_process_uninspectable')
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
                    ('szExeFile', wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    names = {'comfybridge.exe'} | {str(name).lower() for name in resource_names}
    trusted = trusted_setup_pids(setup_path)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        _fail('install_process_uninspectable')
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
            _fail('install_process_uninspectable')
        while True:
            candidate = entry.szExeFile.lower() in names
            process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
            if not process:
                # A snapshot can outlive its process. ERROR_INVALID_PARAMETER is
                # the documented missing-PID result (PID 0 is never a candidate).
                # Access denied and every unknown error remain fail-closed.
                open_error = ctypes.get_last_error()
                if candidate and open_error != 87:
                    _fail('install_process_uninspectable')
            else:
                try:
                    image, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
                    if not kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                        if candidate:
                            # Use the SAME held process handle, never a fresh PID
                            # lookup: PID reuse cannot turn an uncertain live image
                            # into a supposedly terminated candidate.
                            exit_code = wintypes.DWORD()
                            if not kernel.GetExitCodeProcess(process, ctypes.byref(exit_code)) or exit_code.value == 259:
                                _fail('install_process_uninspectable')
                    else:
                        try:
                            image_path = Path(image.value).resolve()
                            if image_path.is_relative_to(root) and entry.th32ProcessID not in trusted:
                                found.append({'pid': entry.th32ProcessID})
                        except (OSError, ValueError, RuntimeError):
                            if candidate:
                                _fail('install_process_uninspectable')
                finally:
                    kernel.CloseHandle(process)
            if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    _fail('install_process_uninspectable')
                break
        return found
    finally:
        kernel.CloseHandle(snapshot)


def check_replace_access(files) -> None:
    """Detection-only exclusive open. Close immediately; never rename/delete/kill."""
    for item in files:
        path = assert_safe_path(item)
        if not path.exists():
            continue
        if not path.is_file():
            _fail()
        if os.name != 'nt':
            if not os.access(path, os.R_OK | os.W_OK):
                _fail('install_resource_uninspectable')
            continue
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000 | 0x10000, 0, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            _fail('install_busy' if error in (32, 33) else 'install_resource_uninspectable')
        kernel.CloseHandle(handle)


def installation_self_test() -> dict:
    """No configuration/data/UI/network; probe only imports and bundled tool versions."""
    import importlib
    import io
    import subprocess
    import sys
    from contextlib import redirect_stdout, redirect_stderr
    from . import __version__
    result = {'product': 'ComfyBridge', 'version': __version__, 'schema': 1, 'ok': False}
    checks = {'imports': False, 'ffmpeg': False, 'ffprobe': False}
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for module in ('fastapi', 'httpx', 'pydantic', 'uvicorn', 'rich', 'comfybridge.api',
                           'comfybridge.service', 'comfybridge.config', 'comfybridge.workflows'):
                importlib.import_module(module)
        checks['imports'] = True
    except Exception:
        return {**result, 'error': 'installation_imports_failed'}
    root = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    roots = [root]
    if getattr(sys, 'frozen', False) and getattr(sys, '_MEIPASS', None):
        roots.append(Path(sys._MEIPASS))
    for tool in ('ffmpeg', 'ffprobe'):
        try:
            candidates = [base / 'bin' / (tool + ('.exe' if os.name == 'nt' else '')) for base in roots]
            executable = next((candidate for candidate in candidates if candidate.is_file()), None)
            if executable is None:
                return {**result, 'error': 'installation_' + tool + '_missing'}
            executable = assert_safe_path(executable, allow_missing=False)
            completed = subprocess.run([str(executable), '-version'], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            if completed.returncode != 0:
                return {**result, 'error': 'installation_' + tool + '_failed'}
            checks[tool] = True
        except Exception:
            return {**result, 'error': 'installation_' + tool + '_failed'}
    return {**result, 'ok': True, 'checks': checks}


def run_installation_self_test() -> int:
    import json
    result = installation_self_test()
    print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    return 0 if result['ok'] else 1


def validate_self_test(executable: Path) -> dict:
    """Run one exact staged binary, accept bounded stable JSON, omit raw diagnostics."""
    import json
    import subprocess
    import tempfile
    executable = assert_safe_path(executable, allow_missing=False)
    if not executable.is_file():
        _fail('install_self_test_failed')
    # A temporary anonymous stream avoids unbounded subprocess PIPE accumulation.
    # It is outside the selected install/data tree and is removed on every exit.
    try:
        with tempfile.TemporaryFile() as output:
            completed = subprocess.run([str(executable), '--installation-self-test'],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=40, check=False, **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            output.seek(0)
            data = output.read(16385)
            if completed.returncode != 0 or len(data) > 16384:
                _fail('install_self_test_failed')
            def unique_pairs(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError('duplicate_key')
                    value[key] = item
                return value
            result = json.loads(data.decode('utf-8'), object_pairs_hook=unique_pairs,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        checks = result.get('checks') if isinstance(result, dict) else None
        if (not isinstance(result, dict) or set(result) != {'product', 'version', 'schema', 'ok', 'checks'}
                or result.get('product') != 'ComfyBridge' or type(result.get('schema')) is not int or result['schema'] != 1
                or result.get('ok') is not True or not isinstance(result.get('version'), str)
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]{0,32})?', result['version'])
                or not isinstance(checks, dict) or set(checks) != {'imports', 'ffmpeg', 'ffprobe'}
                or any(value is not True for value in checks.values())):
            _fail('install_self_test_failed')
        return result
    except InstallPlatformError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        raise InstallPlatformError('install_self_test_failed') from None


class SetupGuard:
    def __init__(self, install_root: Path, data_root: Path, managed_files=(), *, setup_path: Path | None = None, probe_only: bool = False):
        self.root, self.data = Path(install_root), Path(data_root)
        self.setup_path, self.probe_only = setup_path, probe_only
        self.managed_files = tuple(Path(path) for path in managed_files)
        self.stack = None

    def __enter__(self):
        self.root, self.data = validate_local_ntfs(self.root), validate_local_ntfs(self.data)
        assert_no_overlap(self.root, self.data)
        stack = self.stack = ExitStack()
        try:
            for path in (self.root / '.comfybridge-install' / 'install.lease', self.data / '.comfybridge-data.lease'):
                if not self.probe_only or path.exists():
                    stack.enter_context(FileLease(path, exclusive=True, create=not self.probe_only))
            for purpose in ('backend', 'start', 'controller', 'config', 'ui'):
                if not self.probe_only or os.name == 'nt':
                    stack.enter_context(InstanceLock(self.data, purpose))
                else:
                    # Source tests on POSIX must not create legacy lock files while probing.
                    legacy = self.data / 'runtime' / (purpose + '.lock')
                    if legacy.exists():
                        stack.enter_context(FileLease(legacy, exclusive=True, create=False))
            self.recheck()
            return self
        except BaseException as error:
            stack.close()
            if isinstance(error, AlreadyRunning):
                raise InstallPlatformError('install_busy') from None
            raise

    def recheck(self, managed_files=None):
        supplied = self.managed_files if managed_files is None else tuple(Path(item) for item in managed_files)
        owned_leases = {self.root / '.comfybridge-install' / 'install.lease', self.data / '.comfybridge-data.lease'}
        files = []
        for path in supplied:
            checked = assert_safe_path(path)
            if not checked.is_relative_to(self.root) and not checked.is_relative_to(self.data):
                _fail()
            if checked not in owned_leases:
                files.append(checked)
        try:
            trusted = trusted_setup_pids(self.setup_path)
            users = [row for row in restart_manager_users(files) if row.get('pid') not in trusted]
            names = {path.name for path in files if path.suffix.lower() == '.exe'}
            if users or inspect_install_processes(self.root, names, setup_path=self.setup_path):
                _fail('install_busy')
            check_replace_access(files)
        except OSError:
            raise InstallPlatformError('install_resource_uninspectable') from None

    def __exit__(self, *exc):
        if self.stack is not None:
            self.stack.close()
            self.stack = None
