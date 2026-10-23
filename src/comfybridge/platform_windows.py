"""Small platform boundary. Never enumerate/kill processes or change firewall rules."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class AlreadyRunning(RuntimeError):
    pass


def directory_id(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode()).hexdigest()[:32]


def _windows_security():
    from ctypes import wintypes
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid = wintypes.LPWSTR()
        if not adv.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid_text = sid.value
        finally:
            kernel.LocalFree(sid)
    finally:
        kernel.CloseHandle(token)
    return adv, kernel, sid_text


def protect_path(path: Path) -> None:
    """Fail closed: protected DACL allows only current user and SYSTEM."""
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('private_path_is_link')
    if os.name != 'nt':
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    adv, kernel, sid = _windows_security()
    descriptor = ctypes.c_void_p()
    sddl = f'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;{sid})'
    if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def private_runtime(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    runtime = data_dir / 'runtime'
    runtime.mkdir(exist_ok=True)
    protect_path(runtime)
    return runtime


def atomic_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    protect_path(path.parent)
    fd, temp = tempfile.mkstemp(prefix='.private-', dir=path.parent)
    temp_path = Path(temp)
    try:
        protect_path(temp_path)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            fd = -1
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if fd != -1:
            os.close(fd)
        temp_path.unlink(missing_ok=True)


class InstanceLock:
    """OS lifetime lock, separate namespaces for backend/config/controller/start."""
    def __init__(self, data_dir: Path, purpose: str = 'backend'):
        self.data_dir, self.purpose = data_dir, purpose
        self.handle = None
        self.stream = None

    def __enter__(self):
        if os.name == 'nt':
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = self.kernel.CreateMutexW(None, False, f'Local\\ComfyBridge-{directory_id(self.data_dir)}-{self.purpose}')
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result not in (0, 0x80):
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                raise AlreadyRunning(self.purpose + '_locked')
        else:
            import fcntl
            self.stream = (private_runtime(self.data_dir) / (self.purpose + '.lock')).open('a+b')
            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.stream.close()
                self.stream = None
                raise AlreadyRunning(self.purpose + '_locked') from None
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None
        if self.stream:
            self.stream.close()
            self.stream = None


def executable_command(*arguments: str) -> list[str]:
    if getattr(sys, 'frozen', False):
        return [sys.executable, *arguments]
    return [sys.executable, '-m', 'comfybridge', *arguments]


def spawn_detached(arguments: list[str]) -> subprocess.Popen:
    kwargs = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
              'stderr': subprocess.DEVNULL, 'close_fds': True}
    if os.name == 'nt':
        kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(executable_command(*arguments), **kwargs)


def startup_shortcut(data_dir: Path) -> Path:
    if os.name != 'nt' or not os.environ.get('APPDATA'):
        raise RuntimeError('startup_requires_windows')
    return Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup' / f'ComfyBridge-{directory_id(data_dir)}.lnk'


def configure_startup(data_dir: Path, *, enable: bool) -> Path:
    """Only called after explicit CLI confirmation. Normal startup never calls this."""
    shortcut = startup_shortcut(data_dir)
    if not enable:
        shortcut.unlink(missing_ok=True)  # exact per-data-dir owned shortcut, never wildcard
        return shortcut
    if not getattr(sys, 'frozen', False):
        raise RuntimeError('startup_requires_packaged_executable')
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    arguments = subprocess.list2cmdline(['--data-dir', str(data_dir.resolve())])
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + quote(shortcut) + ");"
              "$s.TargetPath=" + quote(sys.executable) + ";$s.Arguments=" + quote(arguments) + ";"
              "$s.WorkingDirectory=" + quote(Path(sys.executable).parent) + ";"
              "$s.Description='ComfyBridge backend and monitor';$s.Save()")
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not shortcut.is_file():
        raise RuntimeError('startup_shortcut_failed')
    return shortcut
