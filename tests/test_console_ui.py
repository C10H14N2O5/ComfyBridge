from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest

from comfybridge import console_ui as c, launcher
from comfybridge.config import AppConfig, ComfyConfig, ServerConfig, save_config, load_config
from comfybridge.control import config_digest, ControlError
from comfybridge.i18n import UIContext, validate_catalog


def settings(tmp_path):
    config = AppConfig(server=ServerConfig(api_key='never-print-this-business-key'),
                       comfyui=ComfyConfig(base_url='http://127.0.0.1:9'))
    save_config(tmp_path, config)
    return config


def home(tmp_path, monkeypatch, answers=(), language='en-US', status=None):
    iterator = iter(answers)
    output = []
    monkeypatch.setattr(c, 'get_status', lambda _: status)
    instance = c.Home(tmp_path, UIContext(language), ask=lambda _: next(iterator), output=output.append)
    return instance, output


def test_catalogs_have_matching_placeholders():
    validate_catalog(c.MESSAGES)
    validate_catalog(launcher.MESSAGES)


def test_main_menu_auto_starts_only_once_and_never_restarts_after_stop(tmp_path, monkeypatch):
    config = settings(tmp_path)
    status = {'config_digest': config_digest(config), 'online': True, 'ready': True}
    instance, output = home(tmp_path, monkeypatch, ['', '3', '1', '', '', '0'], status=status)
    starts, stops = [], []
    monkeypatch.setattr(launcher, 'ensure_backend', lambda path: starts.append(path))
    monkeypatch.setattr(launcher, 'request_stop', lambda path, **kw: stops.append(kw))
    assert instance.run() == 0
    assert len(starts) == 1
    assert stops == [{'force': False}]
    assert all(' | ' not in line for line in output)
    assert not any('never-print-this' in line for line in output)


@pytest.mark.parametrize('exception', [EOFError, KeyboardInterrupt])
def test_close_observer_never_stops_backend(tmp_path, monkeypatch, exception):
    settings(tmp_path)
    instance, _ = home(tmp_path, monkeypatch)
    instance.ask = lambda _: (_ for _ in ()).throw(exception())
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: None)
    monkeypatch.setattr(launcher, 'request_stop', lambda *a, **kw: pytest.fail('observer stopped service'))
    assert instance.run() == 0


def test_failed_start_keeps_repair_menu_and_unknown_state(tmp_path, monkeypatch):
    settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch, ['5', '', '0'])
    def fail(_): raise ControlError('private exception text')
    monkeypatch.setattr(launcher, 'ensure_backend', fail)
    edits = []
    monkeypatch.setattr(c, 'run_wizard', lambda *a, **kw: edits.append(kw['section']) or 1)
    assert instance.run() == 0
    assert edits == ['server']
    assert any('unconfirmed' in line for line in output)
    assert 'private exception text' not in '\n'.join(output)


def test_first_setup_cancel_does_not_launch(tmp_path, monkeypatch):
    instance, output = home(tmp_path, monkeypatch, ['0'])
    monkeypatch.setattr(c, 'run_wizard', lambda *a, **kw: 1)
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: pytest.fail('unconfigured start'))
    assert instance.run() == 0
    assert any('First setup was not saved' in line for line in output)


def test_first_setup_save_then_launch_then_menu(tmp_path, monkeypatch):
    instance, _ = home(tmp_path, monkeypatch, ['0'])
    events = []
    def wizard(path, **kwargs):
        assert kwargs['first_run']
        settings(path)
        events.append('setup')
        return 0
    monkeypatch.setattr(c, 'run_wizard', wizard)
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: events.append('start'))
    assert instance.run() == 0
    assert events == ['setup', 'start']


def test_language_switch_does_not_change_config_digest_or_restart(tmp_path, monkeypatch):
    config = settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch, ['a', '1', '0'])
    starts = []
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: starts.append(True))
    monkeypatch.setattr(launcher, 'request_restart', lambda _: pytest.fail('language restarted backend'))
    assert instance.run() == 0
    assert instance.ui.language == 'zh-CN'
    assert json.loads((tmp_path / 'ui.json').read_text())['language'] == 'zh-CN'
    assert config_digest(load_config(tmp_path)) == config_digest(config)
    assert len(starts) == 1
    assert any('功能选择' in line for line in output)


def test_monitor_returns_to_same_home_context(tmp_path, monkeypatch):
    settings(tmp_path)
    instance, _ = home(tmp_path, monkeypatch, ['1', '0'], 'zh-CN')
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: None)
    calls = []
    monkeypatch.setattr(c, 'monitor', lambda path, **kwargs: calls.append(kwargs))
    assert instance.run() == 0
    assert calls == [{'ui': instance.ui, 'return_to_menu': True, 'reader': instance.reader}]


@pytest.mark.parametrize(('answer', 'expected'), [('', 0), ('0', 0), ('1', 1), ('Y', 1)])
def test_save_requires_separate_restart_confirmation(tmp_path, monkeypatch, answer, expected):
    settings(tmp_path)
    instance, _ = home(tmp_path, monkeypatch, [answer], status={'config_digest': 'old-running-config'})
    calls = []
    monkeypatch.setattr(c, 'run_wizard', lambda *a, **kw: 0)
    monkeypatch.setattr(launcher, 'request_restart', lambda _: calls.append(True))
    instance.edit('server')
    assert len(calls) == expected


def test_save_matching_config_does_not_offer_restart(tmp_path, monkeypatch):
    config = settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch, status={'config_digest': config_digest(config)})
    monkeypatch.setattr(c, 'run_wizard', lambda *a, **kw: 0)
    instance.edit('presets')
    assert any('already matches' in line for line in output)


def test_force_stop_cancel_is_noop(tmp_path, monkeypatch):
    instance, _ = home(tmp_path, monkeypatch, [''])
    monkeypatch.setattr(launcher, 'request_stop', lambda *a, **kw: pytest.fail('cancelled stop'))
    instance.stop(force=True)


def test_uncertain_task_choice_sends_exact_id_after_warning(tmp_path, monkeypatch):
    ident = str(uuid.uuid4())
    instance, output = home(tmp_path, monkeypatch, ['wrong', '2', '1', '1'], status={'unresolved': [
        {'task_id': ident, 'state': 'uncertain', 'error_code': 'submission_uncertain'},
        {'task_id': str(uuid.uuid4()), 'state': 'running'}, {'task_id': '../../bad', 'state': 'uncertain'}]})
    calls = []
    class Client:
        def __init__(self, path): pass
        def command(self, command, body): calls.append((command, body))
    monkeypatch.setattr(c, 'ControlClient', Client)
    instance.resolve()
    assert calls == [('resolve', {'task_id': ident, 'acknowledge': True})]
    assert any('does not prove remote completion' in line for line in output)


def test_no_uncertain_task_does_not_prompt(tmp_path, monkeypatch):
    instance, output = home(tmp_path, monkeypatch, status={'unresolved': []})
    instance.resolve()
    assert any('no uncertain tasks' in line for line in output)


def test_startup_only_modifies_exact_owned_shortcut_after_confirm(tmp_path, monkeypatch):
    shortcut = tmp_path / 'simulated.lnk'
    instance, _ = home(tmp_path, monkeypatch, ['1', '0', '2', '1', '', '0'])
    monkeypatch.setattr(c, 'startup_shortcut', lambda _: shortcut)
    calls = []
    monkeypatch.setattr(c, 'configure_startup', lambda path, **kw: calls.append((path, kw)))
    instance.startup()
    assert calls == [(tmp_path.resolve(), {'enable': False})]
    assert not shortcut.exists()


def test_summary_is_human_readable_and_hides_key(tmp_path, monkeypatch):
    settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch, language='zh-CN')
    instance.summary()
    text = '\n'.join(output)
    assert '配置摘要' in text and '已设置（隐藏）' in text
    assert 'never-print-this' not in text
    assert '"server":' not in text


def test_launcher_no_args_routes_home_without_direct_backend_spawn(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(c, 'run_home', lambda path: calls.append(path) or 0)
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: pytest.fail('launcher bypassed home'))
    assert launcher.main(['--data-dir', str(tmp_path)]) == 0
    assert calls == [tmp_path.resolve()]


def test_status_cli_stays_machine_readable_and_skips_ui_preferences(tmp_path, monkeypatch, capsys):
    import comfybridge.ui_preferences as preferences
    monkeypatch.setattr(preferences, 'resolve_ui', lambda *a, **kw: pytest.fail('status touched language'))
    monkeypatch.setattr(launcher, 'get_status', lambda _: {'ready': True})
    assert launcher.main(['status', '--data-dir', str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {'ready': True}


def test_controller_only_failure_visible_on_home_when_backend_absent(tmp_path, monkeypatch):
    settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch)
    monkeypatch.setattr(launcher, '_controller_status', lambda _: {'restart': {'state': 'failed', 'error': 'restart_failed_check_local_config_and_logs'}})
    instance.render()
    assert any('failed' in line.lower() for line in output)


@pytest.mark.parametrize(('key', 'event'), [('2', 'stop'), ('3', 'restart')])
def test_embedded_monitor_pipe_confirmations_do_not_become_home_actions(tmp_path, monkeypatch, key, event):
    import importlib
    import os
    from comfybridge.console_input import ConsoleInput
    module = importlib.import_module('comfybridge.monitor')
    config = settings(tmp_path)
    status = {'config_digest': config_digest(config), 'online': True, 'ready': True}
    monkeypatch.setattr(c, 'get_status', lambda _: status)
    monkeypatch.setattr(module, 'monitor_snapshot', lambda _: {'backend': status})
    calls = []
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: calls.append('start'))
    monkeypatch.setattr(launcher, 'request_stop', lambda *a, **kw: calls.append('stop'))
    monkeypatch.setattr(launcher, 'request_restart', lambda _: calls.append('restart'))
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, f'1\n{key}\n1\n0\n0\n'.encode())
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, 'rb', buffering=0) as stream:
        reader = ConsoleInput(stream=stream, prompt_writer=lambda _: None)
        instance = c.Home(tmp_path, UIContext(), reader=reader, output=lambda _: None)
        assert instance.run() == 0
    assert calls == ['start', event]


def test_required_language_cancel_never_reaches_setup_or_start(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'ensure_backend', lambda _: pytest.fail('cancelled language started backend'))
    monkeypatch.setattr(c, 'run_wizard', lambda *a, **kw: pytest.fail('cancelled language started setup'))
    assert c.run_home(tmp_path, interactive=True, ask=lambda _: (_ for _ in ()).throw(EOFError()), output=lambda _: None) == 0
    assert not (tmp_path / 'ui.json').exists()


def test_script_start_does_not_prompt_language(tmp_path, monkeypatch):
    import comfybridge.ui_preferences as preferences
    settings(tmp_path)
    (tmp_path / 'ui.json').write_text('{broken', encoding='utf-8')
    monkeypatch.setattr(preferences, 'is_interactive', lambda: False)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('script asked input'))
    starts = []
    monkeypatch.setattr(launcher, 'ensure_backend', lambda path: starts.append(path))
    assert launcher.main(['start', '--data-dir', str(tmp_path)]) == 0
    assert len(starts) == 1
    assert (tmp_path / 'ui.json').read_text() == '{broken'


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
@pytest.mark.parametrize('method', ['free', 'workflow'])
def test_home_idle_cache_summary_shows_settings_without_cleanup_text(tmp_path, monkeypatch, language, method):
    config = settings(tmp_path).model_dump(mode='json')
    config['models'] = [{'id': 'idle-model', 'cache_policy': 'idle_release',
                         'idle_release': {'seconds': 123, 'method': method, 'text': 'PRIVATE-CLEANUP-TEXT'}}]
    save_config(tmp_path, AppConfig.model_validate(config))
    instance, output = home(tmp_path, monkeypatch, language=language)
    instance.summary()
    text = '\n'.join(output)
    assert 'idle-model' in text and '123' in text
    from comfybridge.wizard import MESSAGES as WIZARD_MESSAGES
    assert instance.ui.t(WIZARD_MESSAGES, 'idle_' + method) in text
    assert instance.ui.t(WIZARD_MESSAGES, 'idle_not_vram_proof') in text
    assert 'PRIVATE-CLEANUP-TEXT' not in text
    assert 'never-print-this-business-key' not in text
    assert '"idle_release"' not in text


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_actual_idle_editor_save_requires_separate_default_cancel_restart(tmp_path, monkeypatch, language):
    settings(tmp_path)
    status = {'config_digest': 'old-running-config'}
    answers = ['2', 'm', '4', '300', '1', '1', 's', '1', '']
    instance, output = home(tmp_path, monkeypatch, answers, language=language, status=status)
    monkeypatch.setattr('comfybridge.control.get_status', lambda _: status)
    monkeypatch.setattr(launcher, 'request_restart', lambda *_a: pytest.fail('save cannot silently restart'))
    monkeypatch.setattr(launcher, 'ensure_backend', lambda *_a: pytest.fail('save cannot silently start'))
    instance.edit('models')
    saved = load_config(tmp_path)
    assert saved.models[0].idle_release.method == 'free'
    assert saved.models[0].idle_release.seconds == 300
    assert all(line in output for line in instance.t('apply_restart').split('\n'))


@pytest.mark.parametrize('language', ['zh-CN', 'en-US'])
def test_home_header_and_shared_summary_layout(tmp_path, monkeypatch, language):
    from comfybridge.wizard import summary_lines
    config = settings(tmp_path)
    instance, lines = home(tmp_path, monkeypatch, language=language, status={
        'config_digest': config_digest(config), 'ready': True, 'online': True, 'draining': False})
    instance.render()
    assert lines[0] == '' and lines[2] == '-' * 48
    assert lines[-1] == '-' * 48
    assert all('\n' not in line for line in lines)
    lines.clear()
    instance.summary()
    expected = summary_lines(config.model_dump(mode='json'), instance.ui)
    assert lines[-len(expected):] == expected
    assert 'never-print-this' not in '\n'.join(lines)


# --- Advanced cleanup entries ----------------------------------------------------

def preset_files(tmp_path):
    from comfybridge.wizard import put_model, put_preset
    data = settings(tmp_path).model_dump(mode='json')
    put_model(data, 'm')
    graph = {'1': {'class_type': 'IndexTTS25BaseNode', 'inputs': {'text': 'one'}},
             '2': {'class_type': 'SaveAudio', 'inputs': {'audio': ['1', 0]}}}
    bindings = dict(text_binding={'node_id': '1', 'field': 'text'}, audio_output_node='2')
    used = put_preset(tmp_path, data, graph, model='m', voice='used', **bindings)
    graph['1']['inputs']['text'] = 'two'
    unused = put_preset(tmp_path, data, graph, model='m', voice='unused', **bindings)
    data['presets'] = [p for p in data['presets'] if p['voice'] == 'used']
    save_config(tmp_path, AppConfig.model_validate(data))
    (tmp_path / 'workflows' / 'manual.json').write_text('{}')
    return tmp_path / used['template'], tmp_path / unused['template']


def test_advanced_cleanup_deletes_only_unreferenced_after_confirm(tmp_path, monkeypatch):
    used, unused = preset_files(tmp_path)
    instance, output = home(tmp_path, monkeypatch, ['5', '1', '', '0'])
    instance.advanced()
    assert used.exists() and not unused.exists() and (tmp_path / 'workflows/manual.json').exists()
    assert '  ' + unused.name in output and '  ' + used.name not in output
    assert any('Deleted 1 unused workflow' in line for line in output)


def test_advanced_cleanup_cancel_keeps_everything(tmp_path, monkeypatch):
    used, unused = preset_files(tmp_path)
    instance, _ = home(tmp_path, monkeypatch, [''])
    instance.cleanup_workflows()
    assert used.exists() and unused.exists()


def test_cleanup_nothing_to_do(tmp_path, monkeypatch):
    settings(tmp_path)
    instance, output = home(tmp_path, monkeypatch)
    instance.cleanup_workflows()
    assert any('No workflow files are unreferenced' in line for line in output)


@pytest.mark.parametrize('broken', [b'{"broken": true}', None])
def test_cleanup_refuses_without_valid_config(tmp_path, monkeypatch, broken):
    used, unused = preset_files(tmp_path)
    if broken is None:
        (tmp_path / 'config.json').unlink()
    else:
        (tmp_path / 'config.json').write_bytes(broken)
    instance, output = home(tmp_path, monkeypatch, ['1'])
    instance.cleanup_workflows()
    assert used.exists() and unused.exists()
    assert any('No files were deleted' in line for line in output)


def test_cleanup_locked_by_editor_deletes_nothing(tmp_path, monkeypatch):
    from comfybridge.platform_windows import AlreadyRunning
    _, unused = preset_files(tmp_path)
    class Busy:
        def __init__(self, *_): pass
        def __enter__(self): raise AlreadyRunning('config_locked')
        def __exit__(self, *_): return False
    monkeypatch.setattr(c, 'InstanceLock', Busy)
    instance, output = home(tmp_path, monkeypatch, ['1'])
    instance.cleanup_workflows()
    assert unused.exists() and any('configuration editor' in line for line in output)


def test_cleanup_rechecks_config_changed_during_confirmation(tmp_path, monkeypatch):
    _, unused = preset_files(tmp_path)
    instance, _ = home(tmp_path, monkeypatch)
    def ask(_):
        changed = load_config(tmp_path).model_dump(mode='json')
        changed['presets'][0]['template'] = 'workflows/' + unused.name
        changed['presets'][0]['sha256'] = __import__('hashlib').sha256(unused.read_bytes()).hexdigest()
        save_config(tmp_path, AppConfig.model_validate(changed))
        return '1'
    instance.ask = ask
    instance.cleanup_workflows()
    assert unused.exists()


def test_advanced_runtime_cleanup_after_confirm(tmp_path, monkeypatch):
    settings(tmp_path)
    keep, old = launcher.capture_snapshot(tmp_path), launcher.capture_snapshot(tmp_path)
    (tmp_path / 'runtime' / 'last-good.json').write_text(json.dumps({'snapshot': keep}))
    instance, output = home(tmp_path, monkeypatch, ['6', '1', '', '0'])
    instance.advanced()
    assert sorted(p.name for p in (tmp_path / 'runtime' / 'snapshots').iterdir()) == [keep]
    assert '  snapshots/' + old in output and any('Deleted 1 runtime item' in line for line in output)


def test_runtime_cleanup_cancel_and_unknown_running_backend(tmp_path, monkeypatch):
    settings(tmp_path)
    old = launcher.capture_snapshot(tmp_path)
    instance, output = home(tmp_path, monkeypatch, [''])
    instance.cleanup_runtime()
    assert (tmp_path / 'runtime' / 'snapshots' / old).is_dir()
    monkeypatch.setattr(launcher, 'backend_lock_held', lambda _: True)
    instance.cleanup_runtime()
    assert (tmp_path / 'runtime' / 'snapshots' / old).is_dir()
    assert any('cannot be identified' in line for line in output)


def test_runtime_cleanup_nothing_to_do(tmp_path, monkeypatch):
    instance, output = home(tmp_path, monkeypatch)
    instance.cleanup_runtime()
    assert any('no runtime temporary files' in line for line in output)
