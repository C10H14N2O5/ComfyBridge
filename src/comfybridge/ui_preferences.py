"""Independent local observer preferences; never edits business configuration."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
from typing import Literal

from pydantic import field_validator

from .config import StrictModel, confined_path, parse_json_strict, _staged_file
from .i18n import UIContext
from .platform_windows import AlreadyRunning, InstanceLock

MAX_PREFERENCE_BYTES = 4096

MESSAGES = {
    "choose_language": ("选择界面语言 / Choose interface language", "选择界面语言 / Choose interface language"),
    "chinese": ("[1] 简体中文", "[1] 简体中文"),
    "english": ("[2] English", "[2] English"),
    "cancel": ("[0] 取消 / Cancel", "[0] 取消 / Cancel"),
    "prompt": ("请选择 / Select: ", "请选择 / Select: "),
    "invalid_choice": ("无效选择，请重试。 / Invalid selection; please try again.",
                       "无效选择，请重试。 / Invalid selection; please try again."),
    "invalid_preferences": ("界面语言偏好无法读取，请重新选择。 / Could not read UI preferences; choose a language.",
                            "界面语言偏好无法读取，请重新选择。 / Could not read UI preferences; choose a language."),
    "session_only": ("语言仅对本次会话生效；未能保存界面偏好。", "Language applies to this session only; UI preferences could not be saved."),
}


class UIPreferenceError(ValueError):
    """Sanitized diagnostic; no raw file contents, paths or exception messages."""
    def __init__(self, code: str = "ui_preferences_invalid"):
        self.code = code
        super().__init__(code)


class UIPreferences(StrictModel):
    schema_version: Literal[1] = 1
    language: Literal["zh-CN", "en-US"]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("ui_preferences_version")
        return value


def _preference_path(data_dir: Path) -> Path:
    try:
        path = confined_path(data_dir, "ui.json")
        if path.is_symlink() or path.is_junction():
            raise UIPreferenceError("ui_preferences_unsafe_path")
        if path.exists() and not path.is_file():
            raise UIPreferenceError("ui_preferences_unsafe_path")
        return path
    except UIPreferenceError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise UIPreferenceError("ui_preferences_unsafe_path") from None


def load_ui_preferences(data_dir: Path) -> UIPreferences | None:
    """Read at most 4097 bytes, reject malformed data, never create a directory."""
    path = _preference_path(Path(data_dir))
    try:
        with path.open("rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise UIPreferenceError("ui_preferences_unsafe_path")
            content = stream.read(MAX_PREFERENCE_BYTES + 1)
    except FileNotFoundError:
        return None
    except UIPreferenceError:
        raise
    except OSError:
        raise UIPreferenceError("ui_preferences_read_failed") from None
    if len(content) > MAX_PREFERENCE_BYTES:
        raise UIPreferenceError("ui_preferences_too_large")
    try:
        return UIPreferences.model_validate(parse_json_strict(content))
    except (ValueError, TypeError, RecursionError):
        raise UIPreferenceError("ui_preferences_invalid") from None


def save_ui_preferences(data_dir: Path, preferences: UIPreferences) -> None:
    """Atomic ui.json replacement under the separate, non-waiting ui writer lock."""
    try:
        validated = UIPreferences.model_validate(preferences.model_dump(mode="python"))
        content = (validated.model_dump_json() + "\n").encode("utf-8")
    except (AttributeError, ValueError, TypeError):
        raise UIPreferenceError("ui_preferences_invalid") from None
    if len(content) > MAX_PREFERENCE_BYTES:
        raise UIPreferenceError("ui_preferences_too_large")
    staged = None
    try:
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        with InstanceLock(root, "ui"):
            target = _preference_path(root)
            staged = _staged_file(root, content)
            # Recheck before publication. os.replace replaces a destination link,
            # never follows it; explicit checking also rejects accidental links.
            _preference_path(root)
            os.replace(staged, target)
    except UIPreferenceError:
        raise
    except AlreadyRunning:
        raise UIPreferenceError("ui_preferences_locked") from None
    except (OSError, ValueError, RuntimeError):
        raise UIPreferenceError("ui_preferences_write_failed") from None
    finally:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass  # A failed cleanup must never reveal a raw path/exception.


def select_language(data_dir: Path, ui: UIContext, *, ask=input, output=print,
                    allow_cancel: bool = True) -> bool:
    """Return True for an explicit choice, including session-only write failures.

    Cancellation/EOF/Ctrl-C returns False without changing or persisting a choice.
    The ui lock is held only by save_ui_preferences, never during interactive input.
    """
    try:
        output(ui.t(MESSAGES, "choose_language"))
        output(ui.t(MESSAGES, "chinese"))
        output(ui.t(MESSAGES, "english"))
        if allow_cancel:
            output(ui.t(MESSAGES, "cancel"))
        while True:
            answer = ask(ui.t(MESSAGES, "prompt"))
            choice = answer.strip() if isinstance(answer, str) else ""
            if allow_cancel and choice.lower() in ("0", "q"):
                return False
            if choice not in ("1", "2"):
                output(ui.t(MESSAGES, "invalid_choice"))
                continue
            language = "zh-CN" if choice == "1" else "en-US"
            ui.set_language(language)
            try:
                save_ui_preferences(data_dir, UIPreferences(language=language))
            except UIPreferenceError:
                output(ui.t(MESSAGES, "session_only"))
            return True
    except (EOFError, KeyboardInterrupt):
        return False


def resolve_ui(data_dir: Path, *, interactive: bool, ask=input, output=print) -> UIContext:
    """Use saved language; an aborted required initial choice raises EOFError.

    Interactive callers must not continue into automatic backend startup when the
    user interrupts language selection. Noninteractive fallback remains English.
    """
    invalid = False
    try:
        preferences = load_ui_preferences(data_dir)
    except UIPreferenceError:
        preferences = None
        invalid = True
    if preferences is not None:
        return UIContext(preferences.language)
    ui = UIContext()
    if interactive:
        if invalid:
            output(ui.t(MESSAGES, "invalid_preferences"))
        if not select_language(data_dir, ui, ask=ask, output=output, allow_cancel=False):
            raise EOFError("ui_language_selection_aborted")
    return ui


def _windows_console(stream) -> bool:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    handle = msvcrt.get_osfhandle(stream.fileno())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    mode = wintypes.DWORD()
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)))


def is_interactive() -> bool:
    """Require real console input AND output; NUL, pipes and absent streams fail."""
    try:
        for stream in (sys.stdin, sys.stdout):
            if stream is None or stream.closed or not stream.isatty():
                return False
            if os.name == "nt":
                if not _windows_console(stream):
                    return False
            elif not os.isatty(stream.fileno()):
                return False
        return True
    except (OSError, ValueError, AttributeError):
        return False
