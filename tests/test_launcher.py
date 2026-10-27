import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import pytest

from comfybridge import launcher
from comfybridge.config import AppConfig, ComfyConfig, ServerConfig, save_config, load_config
from comfybridge.control import ControlError


def config(tmp_path, key='a-test-key'):
    value = AppConfig(server=ServerConfig(api_key=key), comfyui=ComfyConfig(base_url='http://127.0.0.1:9'))
    save_config(tmp_path, value)
    return value


def test_snapshot_immutable_pending_config_not_replaced(tmp_path):
    first = config(tmp_path)
    snapshot = launcher.capture_snapshot(tmp_path)
    config(tmp_path, 'different-pending-key')
    assert launcher.snapshot_config(tmp_path, snapshot) == first
    launcher.restore_snapshot_templates(tmp_path, snapshot)
    assert load_config(tmp_path).server.api_key == 'different-pending-key'
    with pytest.raises(ValueError):
        launcher.snapshot_config(tmp_path, '../elsewhere')


def test_last_good_snapshot_restores_exact_immutable_template(tmp_path):
    from comfybridge.wizard import put_model, put_preset
    data = config(tmp_path).model_dump(mode='json')
    put_model(data, 'm')
    graph = {'1': {'class_type': 'IndexTTS25BaseNode', 'inputs': {'text': 'template'}},
             '2': {'class_type': 'SaveAudio', 'inputs': {'audio': ['1', 0]}}}
    preset = put_preset(tmp_path, data, graph, model='m', voice='v',
                        text_binding={'node_id': '1', 'field': 'text'}, audio_output_node='2')
    save_config(tmp_path, AppConfig.model_validate(data))
    snapshot = launcher.capture_snapshot(tmp_path)
    template = (tmp_path / preset['template']).resolve()
    assert template.is_relative_to(tmp_path.resolve())
    original = template.read_bytes()
    template.unlink()
    launcher.restore_snapshot_templates(tmp_path, snapshot)
    assert template.read_bytes() == original


def test_existing_backend_attaches_without_spawning(tmp_path, monkeypatch):
    existing = {'instance_id': 'expected'}
    monkeypatch.setattr(launcher, 'get_status', lambda _: existing)
    monkeypatch.setattr(launcher, 'spawn_detached', lambda *_: pytest.fail('must not spawn'))
    assert launcher.ensure_backend(tmp_path) is existing


def test_wait_start_local_exit_and_unconfirmed_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'get_status', lambda _: None)
    child = type('Child', (), {'poll': lambda _: 1})()
    with pytest.raises(launcher.LocalStartFailure):
        launcher._wait_start(tmp_path, child, 'snapshot')
    child.poll = lambda: None
    with pytest.raises(TimeoutError):
        launcher._wait_start(tmp_path, child, 'snapshot', timeout=0)


def setup_restart(tmp_path, monkeypatch, *, failures=0, timeout=False):
    config(tmp_path)
    old_snapshot = launcher.capture_snapshot(tmp_path)
    old = {'snapshot': old_snapshot}
    monkeypatch.setattr(launcher, 'get_status', lambda _: old)
    commands = []
    class Client:
        def __init__(self, _): self.n = 0
        def command(self, route, body=None): commands.append((route, body))
        def status(self):
            self.n += 1
            return {'drained': self.n > 1}
    monkeypatch.setattr(launcher, 'ControlClient', Client)
    monkeypatch.setattr(launcher.time, 'sleep', lambda _: None)
    starts = []
    def start(_data, snapshot):
        starts.append(snapshot)
        if timeout:
            raise TimeoutError()
        if len(starts) <= failures:
            raise launcher.LocalStartFailure()
        return {'snapshot': snapshot}
    monkeypatch.setattr(launcher, '_start_snapshot', start)
    return old_snapshot, commands, starts


def test_restart_waits_drain_then_starts_once(tmp_path, monkeypatch):
    _, commands, starts = setup_restart(tmp_path, monkeypatch)
    assert launcher.restart_controller(tmp_path) == 0
    assert commands == [('drain', None), ('stop', {'force': False})]
    assert len(starts) == 1
    assert launcher._controller_status(tmp_path)['restart']['state'] == 'completed'


def test_restart_one_last_good_fallback_preserves_pending(tmp_path, monkeypatch):
    old, _, starts = setup_restart(tmp_path, monkeypatch, failures=1)
    config(tmp_path, 'new-pending')
    assert launcher.restart_controller(tmp_path) == 1
    assert len(starts) == 2 and starts[1] == old
    assert load_config(tmp_path).server.api_key == 'new-pending'
    status = launcher._controller_status(tmp_path)['restart']
    assert status['state'] == 'fallback_running'
    assert status['error'] == 'pending_config_start_failed'


def test_restart_fallback_never_recurses(tmp_path, monkeypatch):
    _, _, starts = setup_restart(tmp_path, monkeypatch, failures=20)
    assert launcher.restart_controller(tmp_path) == 1
    assert len(starts) == 2
    assert launcher._controller_status(tmp_path)['restart']['state'] == 'failed'


def test_restart_timeout_does_not_launch_fallback(tmp_path, monkeypatch):
    _, _, starts = setup_restart(tmp_path, monkeypatch, timeout=True)
    assert launcher.restart_controller(tmp_path) == 1
    assert len(starts) == 1


def test_restart_force_stop_cancels_controller(tmp_path, monkeypatch):
    _, _, starts = setup_restart(tmp_path, monkeypatch)
    class Client:
        def __init__(self, _): pass
        def command(self, route, body=None): pass
        def status(self): return {'drained': False, 'force_requested': True}
    monkeypatch.setattr(launcher, 'ControlClient', Client)
    assert launcher.restart_controller(tmp_path) == 1
    assert not starts
    assert launcher._controller_status(tmp_path)['restart']['state'] == 'cancelled'


def test_restart_cancel_before_controller_started_is_not_lost(tmp_path, monkeypatch):
    _, commands, starts = setup_restart(tmp_path, monkeypatch)
    launcher.atomic_private_json(tmp_path / 'runtime/restart-cancel.json', {'cancel': True, 'force': True})
    assert launcher.restart_controller(tmp_path) == 1
    assert not starts
    assert launcher._controller_status(tmp_path)['restart']['state'] == 'cancelled'


def test_stop_arriving_during_new_start_is_honored(tmp_path, monkeypatch):
    _, commands, starts = setup_restart(tmp_path, monkeypatch)
    def start(_data, snapshot):
        starts.append(snapshot)
        launcher.atomic_private_json(tmp_path / 'runtime/restart-cancel.json', {'cancel': True, 'force': True})
        return {'snapshot': snapshot}
    monkeypatch.setattr(launcher, '_start_snapshot', start)
    assert launcher.restart_controller(tmp_path) == 1
    assert len(starts) == 1
    assert commands[-1] == ('stop', {'force': True})
    assert launcher._controller_status(tmp_path)['restart']['state'] == 'cancelled'


def test_admin_resolution_and_force_require_confirm(tmp_path, monkeypatch):
    monkeypatch.setattr('comfybridge.console_input.ConsoleInput.ask', lambda _self, _prompt: 'no')
    monkeypatch.setattr(launcher, 'request_stop', lambda *_args, **_kwargs: pytest.fail('unconfirmed'))
    assert launcher.main(['stop', '--force', '--data-dir', str(tmp_path)]) == 1


def test_parser_data_dir_before_or_after_command(tmp_path):
    assert launcher.parse_arguments(['--data-dir', str(tmp_path), 'status']).data_dir == tmp_path
    assert launcher.parse_arguments(['status', '--data-dir', str(tmp_path)]).data_dir == tmp_path


def test_restart_is_detached_not_monitor_callback(tmp_path, monkeypatch):
    config(tmp_path)
    calls = []
    monkeypatch.setattr(launcher, 'spawn_detached', calls.append)
    launcher.request_restart(tmp_path)
    assert calls == [['_restart-controller', '--data-dir', str(tmp_path.resolve())]]


@pytest.mark.parametrize('command', [[], ['start'], ['monitor'], ['configure'], ['serve'], ['_restart-controller'],
                                     ['status'], ['restart'], ['stop'], ['resolve', 'task-id'], ['startup', 'enable']])
def test_all_packaged_cli_roles_hold_install_and_data_lifetime_leases(tmp_path, monkeypatch, command):
    from comfybridge.install_platform import FileLease, InstallPlatformError
    root, data = tmp_path / 'bundle', tmp_path / 'external-data'
    root.mkdir()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'ComfyBridge.exe'))
    called = []
    def run(args, selected):
        called.append((args.command, selected))
        for path in (root / '.comfybridge-install' / 'install.lease', data / '.comfybridge-data.lease'):
            with pytest.raises(InstallPlatformError, match='install_busy'):
                with FileLease(path, exclusive=True):
                    pytest.fail('role lifetime must prevent installer exclusive acquisition')
        return 7
    monkeypatch.setattr(launcher, '_run_command', run)
    assert launcher.main([*command, '--data-dir', str(data)]) == 7
    assert called == [(command[0] if command else None, data)]
    with FileLease(root / '.comfybridge-install' / 'install.lease', exclusive=True), FileLease(data / '.comfybridge-data.lease', exclusive=True):
        pass


def test_packaged_incomplete_transaction_blocks_before_config_or_status_read(tmp_path, monkeypatch, capsys):
    root, data = tmp_path / 'bundle', tmp_path / 'not-created-data'
    (root / '.comfybridge-install').mkdir(parents=True)
    (root / '.comfybridge-install' / 'transaction.json').write_text('corrupt')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'ComfyBridge.exe'))
    monkeypatch.setattr(launcher, 'get_status', lambda _: pytest.fail('status cannot prove installer-safe'))
    monkeypatch.setattr(launcher, '_run_command', lambda *_: pytest.fail('normal role cannot start'))
    assert launcher.main(['status', '--data-dir', str(data)]) == 1
    assert not data.exists()
    assert 'install_transaction_incomplete' in capsys.readouterr().err


def test_installation_selftest_bypasses_marker_data_ui_and_all_leases(tmp_path, monkeypatch, capsys):
    from comfybridge import install_platform
    root, data = tmp_path / 'bundle', tmp_path / 'not-created'
    (root / '.comfybridge-install').mkdir(parents=True)
    (root / '.comfybridge-install' / 'transaction.json').write_text('in-progress')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(root / 'ComfyBridge.exe'))
    monkeypatch.setattr(install_platform, 'SharedLifetimeLeases', lambda *_: pytest.fail('selftest must bypass leases'))
    monkeypatch.setattr(launcher, '_run_command', lambda *_: pytest.fail('selftest cannot enter business/UI'))
    monkeypatch.setattr(install_platform, 'installation_self_test', lambda: {
        'product': 'ComfyBridge', 'version': '0.4.0', 'schema': 1, 'ok': True,
        'checks': {'imports': True, 'ffmpeg': True, 'ffprobe': True}})
    assert launcher.main(['--installation-self-test', '--data-dir', str(data)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is True and result['schema'] == 1
    assert not data.exists() and not (root / '.comfybridge-install' / 'install.lease').exists()


def test_source_execution_does_not_add_installation_side_effects(tmp_path, monkeypatch):
    from comfybridge import install_platform
    monkeypatch.delattr(sys, 'frozen', raising=False)
    monkeypatch.setattr(install_platform, 'SharedLifetimeLeases', lambda *_: pytest.fail('source does not acquire packaged lease'))
    monkeypatch.setattr(launcher, '_run_command', lambda args, data: 0)
    assert launcher.main(['status', '--data-dir', str(tmp_path / 'data')]) == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.environ.get('COMFYBRIDGE_DESKTOP_SMOKE') != '1', reason='explicit opt-in isolated local process smoke')
def test_real_detached_offline_lifecycle(tmp_path):
    """No generation: target is our bound-but-not-listening loopback socket."""
    from comfybridge.control import ControlClient, get_status
    from comfybridge.platform_windows import spawn_detached, InstanceLock, AlreadyRunning
    offline = socket.socket()
    offline.bind(('127.0.0.1', 0))
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        business_port = reservation.getsockname()[1]
    settings = AppConfig(server=ServerConfig(api_key='isolated-smoke-key', listen_port=business_port),
                         comfyui=ComfyConfig(base_url=f'http://127.0.0.1:{offline.getsockname()[1]}'))
    save_config(tmp_path, settings)
    snapshot = launcher.capture_snapshot(tmp_path)
    child = spawn_detached(['serve', '--data-dir', str(tmp_path), '--snapshot', snapshot])
    controller = None
    try:
        initial = launcher._wait_start(tmp_path, child, snapshot, timeout=15)
        assert initial['running_config']['listen_port'] == business_port
        assert initial['running_config']['api_key'] == '[redacted]'
        assert not initial['ready']
        # Monitor subprocess exits normally; backend survives it.
        code = 'from pathlib import Path; from comfybridge.monitor import monitor; import sys; raise SystemExit(monitor(Path(sys.argv[1]), key_reader=lambda: "q", output=lambda _: None))'
        observer = subprocess.run([sys.executable, '-c', code, str(tmp_path)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        assert observer.returncode == 0
        assert get_status(tmp_path)['instance_id'] == initial['instance_id']
        duplicate = spawn_detached(['serve', '--data-dir', str(tmp_path), '--snapshot', snapshot])
        assert duplicate.wait(timeout=10) != 0
        assert get_status(tmp_path)['instance_id'] == initial['instance_id']
        # Detached controller completes after the monitor has exited.
        controller = launcher.request_restart(tmp_path)
        assert controller.wait(timeout=20) == 0
        assert child.wait(timeout=10) == 0
        current = get_status(tmp_path)
        assert current and current['instance_id'] != initial['instance_id']
        assert launcher._controller_status(tmp_path)['restart']['state'] == 'completed'
        # Local startup failure (our own occupied port) triggers precisely one last-good fallback.
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            settings.server.listen_port = occupied.getsockname()[1]
            save_config(tmp_path, settings)
            controller = launcher.request_restart(tmp_path)
            assert controller.wait(timeout=20) == 1
            fallback = get_status(tmp_path)
            assert fallback and fallback['running_config']['listen_port'] == business_port
            assert launcher._controller_status(tmp_path)['restart']['state'] == 'fallback_running'
            assert load_config(tmp_path).server.listen_port != business_port
        journal = tmp_path / 'runtime/tasks.sqlite3'
        assert journal.exists()
        launcher.request_stop(tmp_path, force=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with InstanceLock(tmp_path):
                    break
            except AlreadyRunning:
                time.sleep(.05)
        else:
            pytest.fail('own backend failed authenticated force-stop')
        assert journal.exists()
    finally:
        if get_status(tmp_path):
            launcher.request_stop(tmp_path, force=True)
        for owned in (controller, child):
            if owned is not None and owned.poll() is None:
                try:
                    owned.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    owned.terminate()  # exact owned Popen handle only, never PID lookup
                    owned.wait(timeout=10)
        offline.close()


@pytest.mark.parametrize('force', [False, True])
def test_cli_confirmation_is_separate_from_prompt_and_blank_cancels(tmp_path, monkeypatch, capsys, force):
    from types import SimpleNamespace
    prompts = []
    monkeypatch.setattr('comfybridge.console_input.ConsoleInput', lambda: SimpleNamespace(
        ask=lambda prompt: prompts.append(prompt) or ''))
    monkeypatch.setattr(launcher, 'request_stop', lambda *_a, **_k: pytest.fail('blank confirmation must cancel'))
    command = ['stop', '--data-dir', str(tmp_path)] + (['--force'] if force else [])
    assert launcher.main(command) == 1
    output = capsys.readouterr().out.splitlines()
    assert '[1] Confirm' in output and '[0] Cancel (default)' in output
    assert prompts == ['Select an option: ']
    if force:
        assert '  - Remote tasks may continue' in output
        assert '  - The journal is retained' in output


@pytest.mark.parametrize('kind,key', [('start', 'local_failure'), ('locked', 'locked'), ('identity', 'identity')])
def test_specific_lifecycle_errors_do_not_add_generic_diagnostic(tmp_path, monkeypatch, capsys, kind, key):
    from comfybridge.control import ControlError
    from comfybridge.platform_windows import AlreadyRunning
    from comfybridge.i18n import UIContext
    error = {'start': launcher.LocalStartFailure, 'locked': AlreadyRunning, 'identity': ControlError}[kind]
    def fail(_):
        raise error('PRIVATE')
    monkeypatch.setattr(launcher, 'ensure_backend', fail)
    monkeypatch.setattr('comfybridge.ui_preferences.is_interactive', lambda: False)
    (tmp_path / 'config.json').write_text('{}', encoding='utf-8')
    assert launcher.main(['start', '--data-dir', str(tmp_path)]) == 1
    output = capsys.readouterr().err
    assert UIContext().t(launcher.MESSAGES, key) in output
    assert 'PRIVATE' not in output and 'Invalid selection' not in output
