"""One reader for console menus, monitor commands and confirmation prompts.

Redirected streams use raw, non-prefetching UTF-8 lines. Mixing input() with
os.read() on the same pipe would let buffered monitor commands become home actions.
"""
from __future__ import annotations

import getpass
import os
import select
import sys


class ConsoleInput:
    def __init__(self, *, stream=None, prompt_writer=None):
        self.stream = sys.stdin if stream is None else stream
        self.prompt_writer = prompt_writer or self._write_prompt
        self.pending = bytearray()
        self.discard_command = False
        self.fd = None
        self.console = False
        try:
            if self.stream is None or self.stream.closed:
                return
            self.fd = self.stream.fileno()
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                import msvcrt
                self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                self.kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
                self.kernel.GetConsoleMode.restype = wintypes.BOOL
                self.kernel.GetFileType.argtypes = [wintypes.HANDLE]
                self.kernel.GetFileType.restype = wintypes.DWORD
                self.kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                self.kernel.PeekNamedPipe.restype = wintypes.BOOL
                self.handle = msvcrt.get_osfhandle(self.fd)
                mode = wintypes.DWORD()
                self.console = bool(self.kernel.GetConsoleMode(self.handle, ctypes.byref(mode)))
            else:
                self.console = os.isatty(self.fd)
        except (OSError, ValueError, AttributeError):
            self.fd = None

    @staticmethod
    def _write_prompt(prompt):
        print(prompt, end='', flush=True)

    def ask(self, prompt: str) -> str:
        if self.console:
            return input(prompt)
        self.prompt_writer(prompt)
        if self.fd is None:
            raise EOFError
        data = self.pending
        self.pending = bytearray()
        while True:
            piece = os.read(self.fd, 1)
            if not piece:
                if not data:
                    raise EOFError
                break
            if piece == b'\n':
                break
            data.extend(piece)
        return bytes(data).rstrip(b'\r').decode('utf-8', errors='strict')

    def secret(self, prompt: str) -> str:
        if self.console:
            return getpass.getpass(prompt)
        # Redirected input has no terminal echo; never print the value or reread
        # through TextIO/getpass, which could prefetch the following commands.
        return self.ask(prompt)

    def _available_byte(self):
        """None=no data yet; b''=EOF; otherwise exactly one byte."""
        if self.fd is None:
            return b''
        try:
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                kind = self.kernel.GetFileType(self.handle)
                if kind == 3:
                    available = wintypes.DWORD()
                    if not self.kernel.PeekNamedPipe(self.handle, None, 0, None, ctypes.byref(available), None):
                        return b''
                    if not available.value:
                        return None
                elif kind != 1:
                    return b''
            elif not select.select([self.fd], [], [], 0)[0]:
                return None
            return os.read(self.fd, 1)
        except (OSError, ValueError):
            return b''

    def poll_key(self) -> str | None:
        if self.console:
            from .monitor import read_key
            return read_key()
        # Commands on redirected streams are lines, unlike immediate real-console
        # keystrokes. Consume the newline BEFORE opening a confirmation/editor.
        for _ in range(256):
            piece = self._available_byte()
            if piece is None:
                return None
            if piece in (b'', b'\n'):
                value = self.pending.decode('ascii', errors='replace').strip().lower()
                self.pending.clear()
                discard = self.discard_command
                self.discard_command = False
                if not discard and len(value) == 1:
                    return value
                return 'q' if piece == b'' else None
            if len(self.pending) < 32:
                self.pending.extend(piece)
            else:
                self.discard_command = True
        return None
