from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from comfybridge.i18n import UIContext, validate_catalog
from comfybridge import ui_preferences as module
from comfybridge.ui_preferences import (
    MAX_PREFERENCE_BYTES, MESSAGES, UIPreferenceError, UIPreferences,
    is_interactive, load_ui_preferences, resolve_ui, save_ui_preferences, select_language,
)


def choices(*answers):
    iterator = iter(answers)
    def ask(_prompt):
        answer = next(iterator)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return ask


def test_preferences_catalog_is_bilingual_and_consistent():
    validate_catalog(MESSAGES)


@pytest.mark.parametrize("value", [
    {}, {"language": "de-DE"}, {"language": "EN-US"}, {"language": None},
    {"language": "en-US", "schema_version": True}, {"language": "en-US", "schema_version": "1"},
    {"language": "en-US", "schema_version": 1.0}, {"language": "en-US", "schema_version": 2},
    {"language": "en-US", "extra": "secret"},
])
def test_preference_model_is_strict(value):
    with pytest.raises(ValidationError):
        UIPreferences.model_validate(value)


def test_missing_preferences_return_none_without_creating_directory(tmp_path):
    data = tmp_path / "does not exist"
    assert load_ui_preferences(data) is None
    assert not data.exists()


def test_roundtrip_language_preserves_business_files(tmp_path):
    sentinel = {
        "config.json": b"PRIVATE BUSINESS CONFIG",
        "config.json.bak": b"PRIVATE BACKUP",
        "runtime/snapshot.json": b"CURRENT RUNNING BACKEND",
    }
    for relative, content in sentinel.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    save_ui_preferences(tmp_path, UIPreferences(language="zh-CN"))
    assert load_ui_preferences(tmp_path).language == "zh-CN"
    save_ui_preferences(tmp_path, UIPreferences(language="en-US"))
    assert load_ui_preferences(tmp_path) == UIPreferences(language="en-US")
    assert json.loads((tmp_path / "ui.json").read_bytes()) == {"schema_version": 1, "language": "en-US"}
    for relative, content in sentinel.items():
        assert (tmp_path / relative).read_bytes() == content
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


@pytest.mark.parametrize("content", [
    b"not JSON PRIVATE SECRET", b'{"language":"en-US","language":"zh-CN"}',
    b'{"language":"en-US","schema_version":NaN}', b'{"language":"en-US","extra":1}',
    b'{"language":null}', b'{"language":"en-US","schema_version":true}',
    b"[]", b"null", b"{}", b'"en-US"', b"\xff", b'{"language":"\\ud800"}',
])
def test_invalid_preference_data_is_sanitized(tmp_path, content):
    (tmp_path / "ui.json").write_bytes(content)
    with pytest.raises(UIPreferenceError) as error:
        load_ui_preferences(tmp_path)
    assert str(error.value) == "ui_preferences_invalid"
    assert str(tmp_path) not in str(error.value)
    assert (tmp_path / "ui.json").read_bytes() == content


def test_preference_read_is_bounded(tmp_path):
    (tmp_path / "ui.json").write_bytes(b" " * (MAX_PREFERENCE_BYTES + 1))
    with pytest.raises(UIPreferenceError, match="ui_preferences_too_large"):
        load_ui_preferences(tmp_path)


def test_preference_payload_boundary_accepts_exact_size(tmp_path):
    content = b'{"language":"en-US"}'
    (tmp_path / "ui.json").write_bytes(content + b" " * (MAX_PREFERENCE_BYTES - len(content)))
    assert load_ui_preferences(tmp_path).language == "en-US"


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_atomic_failure_preserves_previous_preferences(tmp_path, monkeypatch, failure):
    save_ui_preferences(tmp_path, UIPreferences(language="en-US"))
    previous = (tmp_path / "ui.json").read_bytes()
    def fail(*_):
        raise OSError("PRIVATE PATH OR DATA")
    monkeypatch.setattr(module.os, failure, fail)
    with pytest.raises(UIPreferenceError, match="ui_preferences_write_failed"):
        save_ui_preferences(tmp_path, UIPreferences(language="zh-CN"))
    assert (tmp_path / "ui.json").read_bytes() == previous
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


def test_preference_write_uses_separate_short_lock(tmp_path, monkeypatch):
    calls = []
    @contextmanager
    def lock(data_dir, purpose):
        calls.append(("enter", Path(data_dir), purpose))
        yield
        calls.append(("exit", Path(data_dir), purpose))
    monkeypatch.setattr(module, "InstanceLock", lock)
    ui = UIContext()
    def ask(_):
        assert calls == []  # Never hold a lock while waiting for keyboard input.
        return "1"
    assert select_language(tmp_path, ui, ask=ask, output=lambda _: None)
    assert calls == [("enter", tmp_path.resolve(), "ui"), ("exit", tmp_path.resolve(), "ui")]


def test_busy_preference_writer_is_sanitized(tmp_path, monkeypatch):
    @contextmanager
    def locked(*_):
        raise module.AlreadyRunning("PRIVATE LOCK INFORMATION")
        yield
    monkeypatch.setattr(module, "InstanceLock", locked)
    with pytest.raises(UIPreferenceError, match="ui_preferences_locked"):
        save_ui_preferences(tmp_path, UIPreferences(language="en-US"))
    assert not (tmp_path / "ui.json").exists()


def test_real_ui_lock_contention_fails_without_touching_previous_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from comfybridge.platform_windows import InstanceLock
    save_ui_preferences(tmp_path, UIPreferences(language="en-US"))
    previous = (tmp_path / "ui.json").read_bytes()
    # Named Windows mutexes are recursive within one thread, so use another
    # thread to exercise actual lock contention rather than a mocked failure.
    with InstanceLock(tmp_path, "ui"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(save_ui_preferences, tmp_path, UIPreferences(language="zh-CN"))
            with pytest.raises(UIPreferenceError, match="ui_preferences_locked"):
                future.result(timeout=5)
    assert (tmp_path / "ui.json").read_bytes() == previous
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


def test_business_config_lock_does_not_block_language_preferences(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from comfybridge.platform_windows import InstanceLock
    with InstanceLock(tmp_path, "config"):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(save_ui_preferences, tmp_path, UIPreferences(language="zh-CN")).result(timeout=5)
    assert load_ui_preferences(tmp_path).language == "zh-CN"


def test_invalid_mutated_preferences_never_overwrite(tmp_path):
    save_ui_preferences(tmp_path, UIPreferences(language="en-US"))
    previous = (tmp_path / "ui.json").read_bytes()
    bad = UIPreferences.model_construct(language="PRIVATE BAD LANGUAGE")
    with pytest.raises(UIPreferenceError, match="ui_preferences_invalid"):
        save_ui_preferences(tmp_path, bad)
    assert (tmp_path / "ui.json").read_bytes() == previous


def test_preferences_destination_directory_is_rejected(tmp_path):
    (tmp_path / "ui.json").mkdir()
    for operation in (lambda: load_ui_preferences(tmp_path),
                      lambda: save_ui_preferences(tmp_path, UIPreferences(language="en-US"))):
        with pytest.raises(UIPreferenceError, match="ui_preferences_unsafe_path"):
            operation()


def test_preferences_destination_junction_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    data = tmp_path / "data"
    outside.mkdir()
    data.mkdir()
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(data / "ui.json"))
    else:
        (data / "ui.json").symlink_to(outside, target_is_directory=True)
    for operation in (lambda: load_ui_preferences(data),
                      lambda: save_ui_preferences(data, UIPreferences(language="en-US"))):
        with pytest.raises(UIPreferenceError, match="ui_preferences_unsafe_path"):
            operation()
    assert list(outside.iterdir()) == []


def test_preferences_file_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"language":"zh-CN"}', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    try:
        (data / "ui.json").symlink_to(outside)
    except OSError as error:
        pytest.skip(f"file symlinks unavailable: {getattr(error, 'winerror', error.errno)}")
    with pytest.raises(UIPreferenceError, match="ui_preferences_unsafe_path"):
        load_ui_preferences(data)
    with pytest.raises(UIPreferenceError, match="ui_preferences_unsafe_path"):
        save_ui_preferences(data, UIPreferences(language="en-US"))
    assert outside.read_text(encoding="utf-8") == '{"language":"zh-CN"}'


@pytest.mark.parametrize("existing", [None, b"invalid PRIVATE data"])
def test_noninteractive_fallback_never_prompts_or_persists(tmp_path, existing):
    data = tmp_path / "data"
    if existing is not None:
        data.mkdir()
        (data / "ui.json").write_bytes(existing)
    def forbidden(*_):
        pytest.fail("noninteractive language selection attempted")
    ui = resolve_ui(data, interactive=False, ask=forbidden, output=forbidden)
    assert ui.language == "en-US"
    if existing is None:
        assert not data.exists()
    else:
        assert (data / "ui.json").read_bytes() == existing


@pytest.mark.parametrize("interactive", [False, True])
def test_saved_language_avoids_prompt_and_rewrite(tmp_path, interactive):
    save_ui_preferences(tmp_path, UIPreferences(language="zh-CN"))
    before = (tmp_path / "ui.json").stat().st_mtime_ns
    def forbidden(*_):
        pytest.fail("saved choice should not prompt")
    assert resolve_ui(tmp_path, interactive=interactive, ask=forbidden, output=forbidden).language == "zh-CN"
    assert (tmp_path / "ui.json").stat().st_mtime_ns == before


def test_first_selection_is_explicit_bilingual_and_persisted(tmp_path):
    output = []
    ui = resolve_ui(tmp_path, interactive=True, ask=choices("1"), output=output.append)
    assert ui.language == load_ui_preferences(tmp_path).language == "zh-CN"
    assert "[1] 简体中文" in output
    assert "[2] English" in output
    assert not any(line.startswith("[0]") for line in output)


def test_invalid_choice_retries_locally_without_implicit_default(tmp_path):
    output = []
    ui = resolve_ui(tmp_path, interactive=True, ask=choices("", "0", "wrong", "2"), output=output.append)
    assert ui.language == "en-US"
    assert load_ui_preferences(tmp_path).language == "en-US"
    assert sum("Invalid selection" in line for line in output) == 3


def test_invalid_saved_preferences_repair_only_after_explicit_choice(tmp_path):
    original = b'{"language":"PRIVATE BAD VALUE"}'
    (tmp_path / "ui.json").write_bytes(original)
    output = []
    def ask(_):
        assert (tmp_path / "ui.json").read_bytes() == original
        return "2"
    ui = resolve_ui(tmp_path, interactive=True, ask=ask, output=output.append)
    assert ui.language == "en-US"
    assert any("Could not read UI preferences" in line for line in output)
    assert "PRIVATE" not in " ".join(output)
    assert load_ui_preferences(tmp_path).language == "en-US"


@pytest.mark.parametrize("answer", ["0", "q", EOFError(), KeyboardInterrupt()])
def test_cancel_leaves_ui_and_file_unchanged(tmp_path, answer):
    save_ui_preferences(tmp_path, UIPreferences(language="zh-CN"))
    previous = (tmp_path / "ui.json").read_bytes()
    ui = UIContext("zh-CN")
    output = []
    assert not select_language(tmp_path, ui, ask=choices(answer), output=output.append)
    assert ui.language == "zh-CN"
    assert (tmp_path / "ui.json").read_bytes() == previous
    assert any(line.startswith("[0]") for line in output)


@pytest.mark.parametrize("interruption", [EOFError(), KeyboardInterrupt()])
@pytest.mark.parametrize("invalid_existing", [False, True])
def test_first_selection_interruption_aborts_caller_without_persisting(tmp_path, interruption, invalid_existing):
    data = tmp_path / "data"
    if invalid_existing:
        data.mkdir()
        (data / "ui.json").write_bytes(b"invalid previous preference")
    continued = []
    with pytest.raises(EOFError, match="ui_language_selection_aborted"):
        resolve_ui(data, interactive=True, ask=choices(interruption), output=lambda _: None)
        continued.append("backend startup must not be reached")
    assert not continued
    if invalid_existing:
        assert (data / "ui.json").read_bytes() == b"invalid previous preference"
    else:
        assert not data.exists()


def test_write_failure_keeps_selected_session_language_with_warning(tmp_path, monkeypatch):
    def fail(*_):
        raise UIPreferenceError("ui_preferences_write_failed")
    monkeypatch.setattr(module, "save_ui_preferences", fail)
    output = []
    ui = UIContext()
    assert select_language(tmp_path, ui, ask=choices("1"), output=output.append)
    assert ui.language == "zh-CN"
    assert output[-1] == "语言仅对本次会话生效；未能保存界面偏好。"
    assert not (tmp_path / "ui.json").exists()


@pytest.mark.parametrize("stdin,stdout", [(None, None), (SimpleNamespace(closed=True), None),
                                        (SimpleNamespace(closed=False, isatty=lambda: False), None)])
def test_interactive_requires_usable_terminal_streams(monkeypatch, stdin, stdout):
    monkeypatch.setattr(module.sys, "stdin", stdin)
    monkeypatch.setattr(module.sys, "stdout", stdout)
    assert not is_interactive()


def test_interactive_checks_both_input_and_output_handles(monkeypatch):
    stdin = SimpleNamespace(closed=False, isatty=lambda: True, fileno=lambda: 10)
    stdout = SimpleNamespace(closed=False, isatty=lambda: True, fileno=lambda: 11)
    monkeypatch.setattr(module.sys, "stdin", stdin)
    monkeypatch.setattr(module.sys, "stdout", stdout)
    if os.name == "nt":
        monkeypatch.setattr(module, "_windows_console", lambda stream: stream is stdin)
    else:
        monkeypatch.setattr(module.os, "isatty", lambda fd: fd == 10)
    assert not is_interactive()
    if os.name == "nt":
        monkeypatch.setattr(module, "_windows_console", lambda _: True)
    else:
        monkeypatch.setattr(module.os, "isatty", lambda _: True)
    assert is_interactive()


def test_actual_devnull_never_counts_as_interactive():
    # Windows NUL may claim isatty(): GetConsoleMode must reject it. No shell,
    # server, persistent child, Startup entry or GPU work is involved.
    completed = subprocess.run(
        [sys.executable, "-c", "from comfybridge.ui_preferences import is_interactive; raise SystemExit(1 if is_interactive() else 0)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=10, check=False,
    )
    assert completed.returncode == 0
