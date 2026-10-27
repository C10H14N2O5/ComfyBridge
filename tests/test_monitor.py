import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from comfybridge import monitor as module
from comfybridge.i18n import UIContext, validate_catalog


def formatted_log(line, ui):
    return '\n'.join(module.format_log_entry(line, ui)).strip()


def test_quit_monitor_never_stops_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {'ready': True}})
    monkeypatch.setattr('comfybridge.launcher.request_stop', lambda *_a, **_k: pytest.fail('Q must not stop'))
    monkeypatch.setattr('comfybridge.launcher.ensure_backend', lambda *_a, **_k: pytest.fail('monitor must not watchdog'))
    assert module.monitor(tmp_path, key_reader=lambda: 'q', output=lambda _: None) == 0


def test_ctrl_c_monitor_does_not_touch_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    monkeypatch.setattr('comfybridge.launcher.request_stop', lambda *_a, **_k: pytest.fail('CtrlC must not stop'))
    def interrupted(): raise KeyboardInterrupt()
    assert module.monitor(tmp_path, key_reader=interrupted, output=lambda _: None) == 0


def test_bounded_log_tail_strips_terminal_controls(tmp_path):
    path = tmp_path / 'test.log'
    path.write_bytes(b'old\n' * 100000 + b'last\x1b[31m\nfinal\n')
    lines = module.tail_lines(path, count=2, max_bytes=256)
    assert lines == ['last[31m', 'final']
    assert module.tail_lines(tmp_path / 'missing') == []


@pytest.mark.parametrize('piped_input', [b'q\n', b'q', b'', b'\nq\n'])
def test_actual_cli_redirected_pipe_q_and_eof(tmp_path, piped_input):
    # Actual CLI/read_key, not an injected key_reader; no backend is launched.
    result = subprocess.run([sys.executable, '-m', 'comfybridge', 'monitor', '--data-dir', str(tmp_path)],
                            input=piped_input, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert b'ComfyBridge Monitor' in result.stdout
    assert not (tmp_path / 'runtime/control.json').exists()
    assert not (tmp_path / 'runtime/tasks.sqlite3').exists()


@pytest.mark.parametrize('file_input', [b'Q\n', b''])
def test_actual_cli_redirected_file_q_and_eof(tmp_path, file_input):
    source = tmp_path / 'monitor-input.txt'
    source.write_bytes(file_input)
    with source.open('rb') as stream:
        result = subprocess.run([sys.executable, '-m', 'comfybridge', 'monitor', '--data-dir', str(tmp_path)],
                                stdin=stream, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_actual_cli_no_console_devnull_exits(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'comfybridge', 'monitor', '--data-dir', str(tmp_path)],
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr


def test_open_empty_pipe_does_not_block_read_key():
    reader, writer = os.pipe()
    try:
        code = 'from comfybridge.monitor import read_key; print(repr(read_key()))'
        # Keep the writer open with NO bytes. read_key must return None immediately,
        # allowing monitor refresh; treating this as EOF would be incorrect.
        result = subprocess.run([sys.executable, '-c', code], stdin=reader, capture_output=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == b'None'
    finally:
        os.close(reader)
        os.close(writer)


def test_missing_stdin_is_observer_quit(monkeypatch):
    monkeypatch.setattr(sys, 'stdin', None)
    assert module.read_key() == 'q'


def test_monitor_catalog_has_both_languages_and_matching_placeholders():
    validate_catalog(module.MESSAGES)
    for language in ('zh-CN', 'en-US'):
        ui = UIContext(language)
        for label in ('label_inflight', 'label_waiting', 'label_delivery', 'label_slots'):
            assert ui.t(module.MESSAGES, label)


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_status_known_fields_human_readable_and_no_secrets(language):
    ui = UIContext(language)
    status = {'backend': {'ready': False, 'online': True, 'draining': True, 'drained': False,
                         'inflight': 1, 'waiting': 2, 'delivery': 3, 'slots': 4,
                         'api_key': 'KEY_SHOULD_NOT_APPEAR', 'token': 'TOKEN_SHOULD_NOT_APPEAR',
                         'snapshot': 'private/file/path', 'config_digest': 'private-digest',
                         'presets': [{'model': 'model-id', 'voice': 'voice-id', 'ready': False,
                                      'reasons': ['schema_missing_class', 'future_reason']}],
                         'unresolved': [{'task_id': 'task-uuid', 'request_id': 'request-private',
                                         'state': 'uncertain', 'client_state': 'abandoned',
                                         'error_code': 'submission_uncertain', 'input': 'PRIVATE_TEXT'}]},
              'saved_config': 'saved_pending_restart',
              'restart': {'state': 'fallback_running', 'error': 'pending_config_start_failed', 'token': 'OTHER_SECRET'}}
    lines = module.format_status(status, ui)
    text = '\n'.join(lines)
    for private in ('KEY_SHOULD_NOT_APPEAR', 'TOKEN_SHOULD_NOT_APPEAR', 'private/file/path', 'private-digest',
                    'request-private', 'PRIVATE_TEXT', 'OTHER_SECRET', '"backend"', '"ready"'):
        assert private not in text
    for identifier in ('model-id', 'voice-id', 'task-uuid', 'future_reason'):
        assert identifier in text
    if language == 'zh-CN':
        assert '等待重启生效' in text and '不会自动重放' in text and '缺少所需节点类型' in text
    else:
        assert 'pending restart' in text and 'never automatically replayed' in text and 'required node class missing' in text
    assert all('\x1b' not in line for line in lines)


@pytest.mark.parametrize('state', [None, {}, {'backend': None}, {'backend': {'state': 'not_running_no_auto_restart'}}, {'token': 'secret'}])
def test_absent_or_unverified_status_is_not_confidently_stopped(state):
    text = '\n'.join(module.format_status(state, UIContext()))
    assert 'connection unconfirmed' in text
    assert 'no automatic restart' in text
    assert 'verified connection' not in text
    assert 'secret' not in text


def test_status_accepts_direct_control_status_and_bounds_lists():
    status = {'ready': True, 'online': True, 'draining': False,
              'inflight': True, 'waiting': -1, 'delivery': 'private', 'slots': 10**30,
              'presets': [{'model': 'm', 'voice': 'v', 'ready': True}] * 25,
              'unresolved': [{'task_id': 'id', 'state': 'running', 'client_state': 'active'}] * 24}
    text = '\n'.join(module.format_status(status, UIContext()))
    assert 'verified connection' in text
    assert any(line.split() == ['Occupied', 'slots', '—'] for line in text.splitlines())
    assert 'private' not in text
    assert text.count('m / v') == 20
    assert '5 more entries' in text and '4 more entries' in text


def test_status_sanitizes_external_labels_and_handles_bad_fields():
    status = {'ready': True, 'presets': [None, {'model': '[red]name\x1b[31m\nother', 'voice': {'secret': 'hidden'},
                                              'ready': 'yes', 'reasons': ['bad code\x1b', None]}],
              'unresolved': [None, {'task_id': ['hidden'], 'state': {}, 'client_state': []}],
              'restart': {'state': ['hidden'], 'error': {'secret': 'hidden'}}}
    text = '\n'.join(module.format_status(status, UIContext()))
    assert '\x1b' not in text and 'hidden' not in text
    assert '[red]name' in text  # renderer disables Rich markup, preserving literal IDs
    assert 'unknown' in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_localized_logs_allow_only_metadata(language):
    line = json.dumps({'time': '2026-10-23T18:00:00', 'level': 'INFO', 'event': 'submission_intent',
                       'model': 'model-id', 'voice': 'voice-id', 'input_length': 15,
                       'prompt_id': 'task-id', 'state': 'uncertain', 'code': 'future_code',
                       'input': 'PRIVATE_TEXT', 'api_key': 'PRIVATE_KEY', 'message': 'RAW_EXCEPTION',
                       'graph': {'text': 'PRIVATE_GRAPH'}, 'Authorization': 'PRIVATE_AUTH'})
    text = formatted_log(line, UIContext(language))
    for private in ('PRIVATE_TEXT', 'PRIVATE_KEY', 'RAW_EXCEPTION', 'PRIVATE_GRAPH', 'PRIVATE_AUTH'):
        assert private not in text
    assert 'model-id' in text and 'task-id' in text and 'future_code' in text
    assert ('已记录提交意图' if language == 'zh-CN' else 'Submission intent recorded') in text
    assert ('字符数=15' if language == 'zh-CN' else 'characters=15') in text


@pytest.mark.parametrize('line', ['PRIVATE_RAW_TEXT', '{bad', '[]', '{"event":"x","event":"y"}',
                                 '{"event":"bad event PRIVATE"}', '{"event":"x","count":NaN}',
                                 '{"event":"x","message":"' + 'A' * 4096 + '"}'])
def test_nonstructured_or_invalid_logs_are_omitted(line):
    assert formatted_log(line, UIContext()) == 'Unrecognized log entry (details omitted)'


def test_unknown_log_codes_stay_canonical_but_control_and_markup_metadata_do_not():
    line = json.dumps({'event': 'new_canonical_event', 'level': '[red]secret', 'model': '[red]secret',
                       'voice': '\x1b[31msecret', 'code': 'new_canonical_code', 'state': 'future_state',
                       'pid': 3, 'count': 10**30, 'time': 'SECRET_TIMESTAMP'})
    text = formatted_log(line, UIContext())
    assert 'new_canonical_event' in text and 'new_canonical_code' in text and 'future_state' in text
    assert 'secret' not in text and 'SECRET_TIMESTAMP' not in text and '\x1b' not in text
    assert 'PID=3' in text and 'count=' not in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('return_to_menu', [False, True])
@pytest.mark.parametrize('key', ['0', 'Q'])
def test_vertical_controls_and_return_never_mutate_backend(tmp_path, monkeypatch, language, return_to_menu, key):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {'ready': True}})
    for name in ('request_stop', 'request_restart', 'ensure_backend'):
        monkeypatch.setattr('comfybridge.launcher.' + name, lambda *_a, **_k: pytest.fail('observer exit must not mutate backend'))
    lines = []
    assert module.monitor(tmp_path, ui=UIContext(language), return_to_menu=return_to_menu,
                          key_reader=lambda: key, output=lines.append) == 0
    controls = [line for line in lines if line.startswith('[')]
    assert len(controls) == 6
    assert all(line.count('[') == 1 for line in controls)
    assert ('返回主菜单' if language == 'zh-CN' else 'Return to home menu') in controls[-1] if return_to_menu else (
        ('关闭监控' if language == 'zh-CN' else 'Close monitor') in controls[-1])


@pytest.mark.parametrize('key,action', [('1', 'ensure_backend'), ('s', 'ensure_backend')])
def test_numbered_management_and_legacy_aliases(tmp_path, monkeypatch, key, action):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    called = []
    monkeypatch.setattr('comfybridge.launcher.' + action, called.append)
    keys = iter([key, '0'])
    assert module.monitor(tmp_path, key_reader=lambda: next(keys), output=lambda _: None, confirm=lambda _: True) == 0
    assert called == [tmp_path]


# Numbered keys and legacy letter aliases for every action that needs confirmation.
# Case folding is generic (key.lower()); the real-pipe test below covers uppercase 'R'.
DESTRUCTIVE_ACTIONS = [
    ('2', 'request_stop', {'force': False}, 'stop_confirm', 'stop_requested'),
    ('x', 'request_stop', {'force': False}, 'stop_confirm', 'stop_requested'),
    ('5', 'request_stop', {'force': True}, 'force_confirm', 'stop_requested'),
    ('f', 'request_stop', {'force': True}, 'force_confirm', 'stop_requested'),
    ('3', 'request_restart', {}, 'restart_confirm', 'restart_requested'),
    ('r', 'request_restart', {}, 'restart_confirm', 'restart_requested'),
]


@pytest.mark.parametrize('key,action,kwargs,prompt_key,done_key', DESTRUCTIVE_ACTIONS)
@pytest.mark.parametrize('confirmed', [False, True])
def test_destructive_actions_run_only_after_explicit_confirmation(tmp_path, monkeypatch, key, action, kwargs,
                                                                  prompt_key, done_key, confirmed):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    calls, prompts, lines = [], [], []
    for name in ('request_stop', 'request_restart'):
        monkeypatch.setattr('comfybridge.launcher.' + name,
                            lambda *args, _name=name, **kw: calls.append((_name, args, kw)))
    monkeypatch.setattr('comfybridge.launcher.ensure_backend', lambda *_a, **_k: pytest.fail('must not start backend'))
    ui = UIContext('zh-CN')
    keys = iter([key, 'q'])
    def confirm(prompt):
        prompts.append(prompt)
        return confirmed
    assert module.monitor(tmp_path, ui=ui, key_reader=lambda: next(keys), output=lines.append, confirm=confirm) == 0
    assert prompts == [ui.t(module.MESSAGES, 'choice_prompt')]
    assert all(line in lines for line in ui.t(module.MESSAGES, prompt_key).split('\n'))
    assert ui.t(module.MESSAGES, 'confirm_yes') in lines
    assert ui.t(module.MESSAGES, 'confirm_no') in lines
    assert calls == ([(action, (tmp_path,), kwargs)] if confirmed else [])
    assert ui.t(module.MESSAGES, done_key if confirmed else 'cancelled') in lines


# The default confirm is a single lambda shared by every action, so one action suffices.
@pytest.mark.parametrize('answer,accepted', [('', False), (' ', False), ('0', False), ('n', False), ('N', False),
                                           ('unexpected', False), ('1', True), ('Y', True), ('y', True)])
def test_default_confirmation_requires_explicit_yes(tmp_path, monkeypatch, answer, accepted):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    monkeypatch.setattr(module, 'ConsoleInput', lambda: SimpleNamespace(ask=lambda _: answer))
    called = []
    monkeypatch.setattr('comfybridge.launcher.request_restart', called.append)
    keys = iter(['r', '0'])
    assert module.monitor(tmp_path, key_reader=lambda: next(keys), output=lambda _: None) == 0
    assert called == ([tmp_path] if accepted else [])


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_confirmation_prompts_state_safety_warnings(language):
    ui = UIContext(language)
    zh = language == 'zh-CN'
    def text(key):
        return ui.t(module.MESSAGES, key)
    for key in ('stop_confirm', 'force_confirm', 'restart_confirm'):
        assert '\n  - ' in text(key)
        assert '[1]' not in text(key) and '[0]' not in text(key)
    assert '[1]' in text('confirm_yes') and '[0]' in text('confirm_no')
    assert ('默认' if zh else 'default') in text('confirm_no')
    assert ('独立控制器' if zh else 'independent controller') in text('restart_confirm')
    assert ('远端任务可能继续' if zh else 'remote tasks may continue') in text('force_confirm')
    assert ('任务记录将保留' if zh else 'journal is retained') in text('force_confirm')
    assert ('已启动独立重启控制器' if zh else 'Independent restart controller started') in text('restart_requested')


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
def test_interrupted_restart_confirmation_does_not_restart(tmp_path, monkeypatch, interruption):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    monkeypatch.setattr('comfybridge.launcher.request_restart', lambda *_a: pytest.fail('unconfirmed restart'))
    def confirm(_):
        raise interruption()
    assert module.monitor(tmp_path, key_reader=lambda: 'r', output=lambda _: None, confirm=confirm) == 0


@pytest.mark.parametrize('key', ['4', 'c'])
def test_configure_passes_same_ui_and_never_restarts(tmp_path, monkeypatch, key):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    monkeypatch.setattr('comfybridge.launcher.request_restart', lambda *_a: pytest.fail('save must not restart'))
    ui, calls, lines = UIContext('zh-CN'), [], []
    reader = SimpleNamespace(ask=lambda _: '', secret=lambda _: '')
    def wizard(path, **kwargs):
        calls.append((path, kwargs))
        return 0
    monkeypatch.setattr('comfybridge.wizard.run_wizard', wizard)
    keys = iter([key, '0'])
    module.monitor(tmp_path, ui=ui, key_reader=lambda: next(keys), output=lines.append, reader=reader)
    assert calls == [(tmp_path, {'ui': ui, 'ask': reader.ask, 'secret': reader.secret, 'output': lines.append})]
    assert any('不会自动重启' in line for line in lines)


def test_action_error_does_not_echo_exception_text_and_monitor_remains_usable(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: None)
    def fail(_):
        raise RuntimeError('RAW_PRIVATE_ERROR')
    monkeypatch.setattr('comfybridge.launcher.ensure_backend', fail)
    keys, lines = iter(['1', 'q']), []
    module.monitor(tmp_path, key_reader=lambda: next(keys), output=lines.append)
    text = '\n'.join(lines)
    assert 'RAW_PRIVATE_ERROR' not in text
    assert 'Operation failed' in text
    assert text.count('ComfyBridge Monitor') == 2


@pytest.mark.parametrize('commands,accepted', [(b'3\n0\nq\n', False), (b'3\n1\nq\n', True),
                                              (b'r\n\nq\n', False), (b'R\nY\n0\n', True)])
def test_actual_pipe_restart_confirmation_reads_next_line_not_command_newline(tmp_path, commands, accepted):
    code = '''
from pathlib import Path
import sys
from comfybridge import monitor, launcher
monitor.monitor_snapshot = lambda _: {'backend': {}}
launcher.request_restart = lambda _: print('RESTART_CALLED')
monitor.monitor(Path(sys.argv[1]), output=lambda _: None)
print('OBSERVER_DONE')
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path)], input=commands,
                            capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert (b'RESTART_CALLED' in result.stdout) is accepted
    assert b'OBSERVER_DONE' in result.stdout
    assert not (tmp_path / 'runtime').exists()


def test_shared_reader_wizard_consumes_only_its_inputs_then_returns_to_monitor(tmp_path, monkeypatch):
    from comfybridge.console_input import ConsoleInput
    source = tmp_path / 'input.txt'
    source.write_bytes(b'4\nmodel-id\nprivate-key\nq\n')
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    seen, lines = [], []
    def wizard(path, *, ui, ask, secret, output):
        seen.extend([ask('model:'), secret('key:')])
        output('wizard-returned')
        return 0
    monkeypatch.setattr('comfybridge.wizard.run_wizard', wizard)
    with source.open('rb') as stream:
        reader = ConsoleInput(stream=stream, prompt_writer=lambda _: None)
        assert module.monitor(tmp_path, reader=reader, output=lines.append) == 0
    assert seen == ['model-id', 'private-key']
    assert 'wizard-returned' in lines
    assert 'private-key' not in '\n'.join(lines)
    assert not (tmp_path / 'runtime').exists()


def test_explicit_key_reader_still_overrides_shared_reader_poll(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {}})
    reader = SimpleNamespace(poll_key=lambda: pytest.fail('injected key_reader must win'))
    assert module.monitor(tmp_path, reader=reader, key_reader=lambda: 'q', output=lambda _: None) == 0


def test_rich_output_explicitly_disables_markup_and_highlighting(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'ready': True, 'presets': [{'model': '[red]id', 'voice': 'v'}]})
    calls = []
    class Console:
        is_terminal = False
        def print(self, text, **kwargs):
            calls.append((text, kwargs))
    monkeypatch.setattr('rich.console.Console', Console)
    module.monitor(tmp_path, key_reader=lambda: 'q')
    assert calls and all(kwargs['markup'] is False and kwargs['highlight'] is False for _, kwargs in calls)
    assert any('[red]id' in text for text, _ in calls)


def test_refresh_uses_shared_language_context_without_stale_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'ready': True})
    ui, lines = UIContext(), []
    monkeypatch.setattr('comfybridge.launcher.ensure_backend', lambda _: ui.set_language('zh-CN'))
    keys = iter(['s', 'q'])
    module.monitor(tmp_path, ui=ui, key_reader=lambda: next(keys), output=lines.append)
    assert 'ComfyBridge Monitor' in lines
    assert 'ComfyBridge 实时监控' in lines
    assert '后台已启动或已连接。' in lines


def cache_row(**changes):
    return {'model': 'model-id', 'method': 'workflow', 'seconds': 300, 'remaining_seconds': 25.5,
            'state': 'countdown', 'error_code': None, 'voice': 'last-voice', **changes}


# Explicit contracts: a production allowlist addition/removal must be reviewed here.
CACHE_STATES = ('pending_idle', 'countdown', 'waiting_remote', 'releasing',
                'requested', 'completed', 'failed', 'uncertain', 'stale')
CACHE_ERROR_CODES = ('cache_remote_busy', 'cache_remote_unavailable', 'cache_config_stale',
                     'cache_build_failed', 'cache_free_uncertain', 'cache_free_failed',
                     'cache_cleanup_failed', 'cache_deadline_exceeded', 'cache_not_active',
                     'cache_admin_resolved', 'cache_release_uncertain', 'submission_uncertain',
                     'cache_seed_exhausted')
CACHE_LOG_EVENTS = ('cache_release_intent', 'cache_release_requested', 'cache_release_completed',
                    'cache_release_failed', 'cache_release_uncertain')


def test_cache_vocabularies_match_monitor_allowlists():
    for values, allowed in ((CACHE_STATES, module._CACHE_STATES),
                            (CACHE_ERROR_CODES, module._CACHE_ERRORS),
                            (CACHE_LOG_EVENTS, module._CACHE_EVENTS)):
        assert len(values) == len(set(values)), values
        assert set(values) == allowed, values


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_idle_cache_states_are_localized_recognized_metadata(language):
    ui = UIContext(language)
    for state in CACHE_STATES:
        status = {'ready': True, 'cache': [cache_row(state=state)]}
        text = '\n'.join(module.format_status(status, ui))
        assert ui.t(module.MESSAGES, 'cache_state_' + state) in text, state
        assert 'model-id' in text and 'last-voice' in text and '25.5' in text and '300' in text, state
        assert ui.t(module.MESSAGES, 'cache_method_workflow') in text, state


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('state', ['requested', 'completed'])
def test_free_acceptance_never_claims_confirmed_unload_or_workflow_completion(language, state):
    ui = UIContext(language)
    text = '\n'.join(module.format_cache_status([cache_row(method='free', state=state, remaining_seconds=None)], ui))
    assert ui.t(module.MESSAGES, 'cache_state_requested') in text
    assert ui.t(module.MESSAGES, 'cache_state_completed') not in text
    assert ('不证明显存已释放' if language == 'zh-CN' else 'does not prove VRAM was freed') in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_cache_status_unknown_fields_codes_and_shapes_never_echo_private_text(language):
    ui = UIContext(language)
    rows = [cache_row(method='PRIVATE-METHOD', state='synthetic_private_state', error_code='synthetic_private_error',
                      seconds='PRIVATE-SECONDS', remaining_seconds='PRIVATE-REMAINING',
                      text='PRIVATE-TEXT', token='PRIVATE-TOKEN', graph={'text': 'PRIVATE-GRAPH'}),
            cache_row(model={'secret': 'PRIVATE-MODEL'}, voice=['PRIVATE-VOICE'], method={'secret': 'PRIVATE-METHOD'},
                      state={'secret': 'PRIVATE-STATE'}, error_code={'secret': 'PRIVATE-ERROR'})]
    text = '\n'.join(module.format_cache_status(rows, ui))
    assert 'PRIVATE' not in text and 'synthetic_private' not in text
    assert ui.t(module.MESSAGES, 'unknown') in text
    assert 'model-id' in text  # only known metadata identifiers survive


@pytest.mark.parametrize('remaining', [float('nan'), float('inf'), -1, True, 86401, {}, '123'])
def test_cache_remaining_seconds_requires_finite_bounded_numeric_value(remaining):
    text = '\n'.join(module.format_cache_status([cache_row(remaining_seconds=remaining)], UIContext()))
    assert 'remaining —s' in text


@pytest.mark.parametrize('seconds', [0, -1, 86401, True, 1.5, {}, '300'])
def test_cache_timeout_requires_strict_bounded_integer(seconds):
    text = '\n'.join(module.format_cache_status([cache_row(seconds=seconds)], UIContext()))
    assert 'idle timeout —s' in text


def test_cache_countdown_accepts_boundary_values_and_bounds_rows():
    rows = [cache_row(seconds=86400, remaining_seconds=0)] * 25
    text = '\n'.join(module.format_cache_status(rows, UIContext()))
    assert text.count('model-id') == 20
    assert '5 more entries' in text
    assert 'remaining 0.0s' in text
    assert module.format_cache_status(None, UIContext()) == []
    assert module.format_cache_status({'text': 'private'}, UIContext()) == []
    assert module.format_cache_status([], UIContext()) == []


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_cache_error_codes_are_localized_without_raw_responses(language):
    ui = UIContext(language)
    for code in CACHE_ERROR_CODES:
        text = '\n'.join(module.format_cache_status([cache_row(error_code=code)], ui))
        assert ui.t(module.MESSAGES, 'code_' + code) in text, code


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_cache_log_events_are_safe_and_localized(language):
    ui = UIContext(language)
    for event in CACHE_LOG_EVENTS:
        raw = json.dumps({'event': event, 'level': 'INFO', 'model': 'm', 'prompt_id': 'uuid',
                          'state': 'uncertain', 'code': 'cache_free_uncertain', 'count': 1,
                          'text': 'PRIVATE-CLEANUP', 'input': 'PRIVATE-TEXT', 'message': 'PRIVATE-EXCEPTION',
                          'token': 'PRIVATE-TOKEN', 'graph': {'text': 'PRIVATE-GRAPH'}})
        text = formatted_log(raw, ui)
        assert ui.t(module.MESSAGES, 'event_' + event) in text, event
        assert 'PRIVATE' not in text, event
        assert ui.t(module.MESSAGES, 'cache_state_uncertain') in text, event


def test_cache_unknown_log_event_state_error_never_echoed():
    ui = UIContext()
    assert formatted_log('{"event":"cache_synthetic_private_token"}', ui) == ui.t(module.MESSAGES, 'log_omitted')
    text = formatted_log(json.dumps({'event': 'cache_release_failed',
                                            'state': 'synthetic_private_state', 'code': 'synthetic_private_error'}), ui)
    assert 'synthetic_private' not in text
    assert 'unknown' in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_monitor_cache_display_does_not_trigger_release_or_change_lifecycle(tmp_path, monkeypatch, language):
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {'ready': True, 'cache': [cache_row()]}})
    for name in ('ensure_backend', 'request_stop', 'request_restart'):
        monkeypatch.setattr('comfybridge.launcher.' + name, lambda *_a, **_k: pytest.fail('status rendering must be read-only'))
    lines = []
    assert module.monitor(tmp_path, ui=UIContext(language), key_reader=lambda: 'q', output=lines.append) == 0
    assert any('model-id' in line for line in lines)


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_stale_cache_waits_for_new_business_generation_not_probe_recovery(language):
    text = '\n'.join(module.format_cache_status([cache_row(state='stale', remaining_seconds=None)], UIContext(language)))
    assert ('等待新的业务生成' if language == 'zh-CN' else 'waiting for a new business generation') in text
    assert ('不会仅靠自动探测恢复' if language == 'zh-CN' else 'not automatic probe recovery') in text
    assert '等待后台核对配置' not in text
    assert 'awaiting backend configuration checks' not in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_durable_seed_failure_status_and_log_are_allowlisted(language):
    ui = UIContext(language)
    expected = ui.t(module.MESSAGES, 'code_cache_seed_exhausted')
    status = '\n'.join(module.format_cache_status([cache_row(state='failed', error_code='cache_seed_exhausted')], ui))
    log = formatted_log(json.dumps({'event': 'cache_release_failed', 'state': 'failed',
                                           'code': 'cache_seed_exhausted', 'seed': 'PRIVATE-COUNTER'}), ui)
    assert expected in status and expected in log
    assert ('未提交清理任务' if language == 'zh-CN' else 'no cleanup task submitted') in expected
    assert ('或无效' if language == 'zh-CN' else 'or invalid') in expected
    assert 'PRIVATE-COUNTER' not in log


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_log_entries_have_header_and_indented_metadata(language):
    ui = UIContext(language)
    raw = json.dumps({'time': '2026-10-27T21:00:00', 'level': 'INFO', 'event': 'submission_intent',
                      'model': 'm', 'voice': 'v', 'input_length': 15})
    lines = module.format_log_entry(raw, ui)
    assert len(lines) == 2
    assert lines[0].startswith('  2026-10-27 21:00:00  ')
    assert ui.t(module.MESSAGES, 'event_submission_intent') in lines[0]
    assert lines[1].startswith('      ')
    assert all('\n' not in line and ' | ' not in line for line in lines)
    assert len(module.format_log_entry('{"event":"service_started"}', ui)) == 1


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_monitor_sections_and_default_ten_entry_limit(tmp_path, monkeypatch, language):
    ui = UIContext(language)
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': {
        'ready': True, 'online': True, 'draining': False, 'presets': [], 'unresolved': [], 'cache': [cache_row()]}})
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs/comfybridge.log').write_text('\n'.join(
        json.dumps({'event': 'service_started', 'count': number}) for number in range(12)), encoding='utf-8')
    lines = []
    module.monitor(tmp_path, ui=ui, key_reader=lambda: 'q', output=lines.append)
    assert lines[:3] == ['', ui.t(module.MESSAGES, 'title'), '-' * 48]
    for key in ('status_title', 'tasks_title', 'cache_title'):
        index = lines.index(ui.t(module.MESSAGES, key))
        assert lines[index - 1] == ''
    assert lines.count('  ' + ui.t(module.MESSAGES, 'event_service_started')) == 10
    assert ui.t(module.MESSAGES, 'logs', count=10) in lines
    assert all('\n' not in line for line in lines)
