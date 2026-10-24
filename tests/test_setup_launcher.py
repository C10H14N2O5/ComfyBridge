from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from comfybridge import setup_launcher as setup
from comfybridge.i18n import validate_catalog


class ScriptedReader:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        try:
            return next(self.answers)
        except StopIteration:
            raise EOFError()


class CoreError(RuntimeError):
    def __init__(self, code, detail='PRIVATE-CONFIG-KEY AND PRIVATE-EXCEPTION'):
        self.code = code
        super().__init__(detail)


class FakeInstaller:
    def __init__(self, context, *, old_version=None, new_version='0.4.0', backup=True):
        self.context = context
        self.old_version = old_version
        self.new_version = new_version
        self.backup = backup
        self.calls = []
        self.failures = {}
        self.modified = ('_internal/modified.dll',)
        self.warnings = ()
        self.recovery = SimpleNamespace(recovered=False, outcome='none', backup_dir=None)
        self.selftest_passed = True
        self.plans = []
        self.results = []
        self.deleted = []

    def called(self, name):
        self.calls.append(name)
        errors = self.failures.get(name, [])
        if errors:
            error = errors.pop(0)
            if error is not None:
                raise error

    def recover(self):
        self.called('recover')
        return self.recovery

    def inspect(self):
        self.called('inspect')
        modes = ('install',) if self.old_version is None else (('repair',) if self.old_version == self.new_version else ('update',))
        return SimpleNamespace(root=self.context.root, target_version=self.new_version,
                               installed_version=self.old_version, modes=modes,
                               managed_modified=self.modified, unknown_files=(), recovery_required=False)

    def plan(self, mode, data_dir=None):
        self.called('plan')
        plan = SimpleNamespace(root=self.context.root, mode=mode, old_version=self.old_version,
                               new_version=self.new_version, data_dir=data_dir,
                               backup_dir=self.context.root / 'ComfyBridge-backups/current-transaction' if self.backup else None,
                               managed_modified=self.modified, warnings=self.warnings, transaction_id='current-transaction')
        self.plans.append(plan)
        return plan

    def execute(self, plan, progress=None):
        self.called('execute')
        assert plan in self.plans
        if progress:
            for stage in ('validate', 'stage', 'self_test', 'backup', 'apply', 'verify', 'commit', 'cleanup', 'PRIVATE-STAGE'):
                progress(stage, 1, 1)
        result = SimpleNamespace(root=self.context.root, version=self.new_version, mode=plan.mode,
                                 data_dir=plan.data_dir, backup_dir=plan.backup_dir,
                                 transaction_id=plan.transaction_id, selftest_passed=self.selftest_passed)
        self.results.append(result)
        return result

    def delete_backup(self, result):
        self.called('delete_backup')
        assert any(result is item for item in self.results)
        self.deleted.append(result)


@pytest.fixture
def context(tmp_path):
    root = tmp_path / '中文 安装目录'
    root.mkdir()
    bundle = tmp_path / 'extracted bundle'
    bundle.mkdir()
    payload = bundle / 'setup-payload.zip'
    manifest = bundle / 'setup-manifest.json'
    executable = root / 'ComfyBridge-Setup.exe'
    payload.write_bytes(b'test fake payload')
    manifest.write_text('{}')
    executable.write_bytes(b'test fake executable')
    return setup.SetupContext(root, payload, manifest, executable)


def run(context, answers, *, core=None):
    core = core or FakeInstaller(context)
    reader = ScriptedReader(answers)
    lines = []
    result = setup.main([], context=context, installer_factory=lambda received: core,
                        reader=reader, output=lines.append)
    return result, core, reader, '\n'.join(lines)


def test_catalog_bilingual_placeholder_parity():
    validate_catalog(setup.MESSAGES)


def test_packaged_root_is_actual_executable_directory_not_cwd_or_meipass(context, tmp_path, monkeypatch):
    unrelated = tmp_path / 'unrelated working directory'
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(context.setup_path))
    monkeypatch.setattr(sys, '_MEIPASS', str(context.payload_zip.parent), raising=False)
    resolved = setup.SetupContext.packaged()
    assert resolved == context
    assert resolved.root != Path.cwd() and resolved.root != Path(sys._MEIPASS)


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction rejection')
@pytest.mark.parametrize('component', ['executable', 'bundle'])
def test_packaged_context_rejects_junction_before_resolving(context, tmp_path, monkeypatch, component):
    from comfybridge.install_platform import InstallPlatformError
    target = context.root if component == 'executable' else context.payload_zip.parent
    junction = tmp_path / 'owned junction alias'
    created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target)],
                             capture_output=True, timeout=5)
    assert created.returncode == 0, created.stderr
    assert junction.is_junction()
    try:
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', str(junction / context.setup_path.name if component == 'executable' else context.setup_path))
        monkeypatch.setattr(sys, '_MEIPASS', str(junction if component == 'bundle' else context.payload_zip.parent), raising=False)
        with pytest.raises(InstallPlatformError) as caught:
            setup.SetupContext.packaged()
        assert caught.value.code == 'install_path_unsafe'
    finally:
        # Remove only this verified lexical junction entry, never its resolved
        # target directory. Both target trees remain owned pytest fixture data.
        assert junction.absolute().parent == tmp_path.absolute() and junction.name == 'owned junction alias'
        assert junction.is_junction()
        junction.rmdir()
    assert target.is_dir() and context.setup_path.is_file()


def test_source_requires_explicit_context_and_never_uses_working_directory(monkeypatch):
    monkeypatch.delattr(sys, 'frozen', raising=False)
    with pytest.raises(setup.SetupUIError) as caught:
        setup.SetupContext.packaged()
    assert caught.value.code == 'setup_source_context'


@pytest.mark.parametrize('missing', ['setup-payload.zip', 'setup-manifest.json'])
def test_missing_embedded_bundle_fails_before_installer(context, monkeypatch, missing):
    target = (context.payload_zip.parent / missing).resolve()
    assert target.parent == context.payload_zip.parent.resolve()
    target.unlink()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(context.setup_path))
    monkeypatch.setattr(sys, '_MEIPASS', str(context.payload_zip.parent), raising=False)
    lines = []
    assert setup.main([], reader=ScriptedReader([]), output=lines.append,
                      installer_factory=lambda _: pytest.fail('untrusted/missing bundle')) == 1
    assert any('embedded installation bundle' in line for line in lines)


@pytest.mark.parametrize('arguments', [['--target', 'C:\\other'], ['install'], ['update', '--data-dir', 'C:\\private']])
def test_arbitrary_cli_targets_and_actions_are_rejected(context, arguments):
    lines = []
    assert setup.main(arguments, context=context, reader=ScriptedReader([]), output=lines.append,
                      installer_factory=lambda _: pytest.fail('CLI must not instantiate installer')) == 2
    text = '\n'.join(lines)
    assert 'command-line install target' in text
    assert 'C:\\private' not in text


def test_help_is_read_only_and_does_not_require_bundle():
    lines = []
    assert setup.main(['--help'], installer_factory=lambda _: pytest.fail('help instantiated installer'),
                      reader=ScriptedReader([]), output=lines.append) == 0
    assert 'Setup' in lines[0] and '目录' in lines[0]


def test_top_menu_enter_and_invalid_input_do_not_implicitly_install(context):
    result, core, _, text = run(context, ['', 'invalid', '0'])
    assert result == 0
    assert core.calls == ['recover', 'inspect']
    assert '无效选项' in text and 'Invalid choice' in text
    assert not (context.root / 'ui.json').exists()
    assert not (context.root / 'config.json').exists()


def test_fresh_install_confirmation_and_default_backup_keep(context):
    result, core, reader, text = run(context, ['1', '1', '', ''])
    assert result == 0
    assert core.plans[0].mode == 'install'
    assert core.plans[0].root == context.root
    assert core.plans[0].data_dir == context.root / 'data'
    assert core.calls[:2] == ['recover', 'inspect']
    assert len(core.results) == 1 and not core.deleted
    assert 'File installation and local self-test passed' in text
    assert 'does not verify real TTS' in text
    assert 'backend was not automatically started' in text
    assert '完整内容' in text and 'full prior installation' in text
    assert '以前的备份' in text and 'earlier backups' in text
    assert '_internal/modified.dll' in text
    assert 'PRIVATE-STAGE' not in text
    assert 'Press Enter to close' in reader.prompts[-1]


@pytest.mark.parametrize('answer', ['', '0', 'n', 'q'])
def test_install_confirmation_defaults_to_cancel(context, answer):
    result, core, _, text = run(context, ['1', answer, '0'])
    assert result == 0 and not core.results and not core.deleted
    assert 'Cancelled' in text and '取消' in text


def test_update_default_data_backup_keep_transcript(context):
    core = FakeInstaller(context, old_version='0.3.0')
    result, core, _, text = run(context, ['2', '', '1', '0', ''], core=core)
    assert result == 0
    assert core.plans[0].mode == 'update'
    assert core.plans[0].data_dir == context.root / 'data'
    assert '0.3.0' in text and '0.4.0' in text
    assert not core.deleted


def test_external_data_is_absolute_backup_only_and_never_install_target(context, tmp_path):
    core = FakeInstaller(context, old_version='0.3.0')
    external = tmp_path / '外部 数据'
    external.mkdir()
    result, core, _, text = run(context, ['2', '2', 'relative/path', str(external), '1', '', ''], core=core)
    assert result == 0
    assert core.plans[0].data_dir == external
    assert core.plans[0].root == context.root
    assert 'absolute path' in text
    assert 'does not change configuration, launch arguments or --data-dir' in text
    assert '不会修改配置' in text


@pytest.mark.parametrize('answers', [['2', '0', '0'], ['2', '2', '0', '0']])
def test_data_selection_cancel_prevents_plan(context, answers):
    result, core, _, _ = run(context, answers, core=FakeInstaller(context, old_version='0.3.0'))
    assert result == 0 and not core.plans and not core.results


def test_same_version_update_is_explicit_repair(context):
    result, core, _, text = run(context, ['2', '', '1', '0', ''], core=FakeInstaller(context, old_version='0.4.0'))
    assert result == 0 and core.plans[0].mode == 'repair'
    assert 'versions are identical' in text and '不是普通升级' in text
    assert 'Repair this same version' in text


def test_same_version_repair_confirmation_defaults_to_cancel(context):
    result, core, _, _ = run(context, ['2', '', '', '0'], core=FakeInstaller(context, old_version='0.4.0'))
    assert result == 0 and not core.results


def test_downgrade_rejected_by_core_never_executes(context):
    core = FakeInstaller(context, old_version='0.5.0')
    core.failures['plan'] = [CoreError('installer_downgrade')]
    result, core, _, text = run(context, ['2', '', ''], core=core)
    assert result == 1 and not core.results
    assert 'Downgrades are not allowed' in text and '不允许降级' in text
    assert 'PRIVATE' not in text


@pytest.mark.parametrize('method', ['recover', 'inspect', 'plan', 'execute'])
def test_busy_recheck_does_not_kill_or_start_and_retries_same_safe_action(context, method):
    core = FakeInstaller(context)
    core.failures[method] = [CoreError('installer_busy'), None]
    prefix = ['1'] if method in ('recover', 'inspect') else []
    answers = prefix + ['1']
    if method == 'plan':
        answers += ['1']
    answers += ['1']
    if method == 'execute':
        answers += ['1']
    answers += ['0', '']
    result, core, _, text = run(context, answers, core=core)
    assert result == 0 and len(core.results) == 1
    assert core.calls.count(method) >= 2
    assert 'manually stop' in text and 'no process will be forcibly ended' in text
    assert 'PRIVATE' not in text


@pytest.mark.parametrize('code', ['installer_busy', 'install_busy', 'install_process_uninspectable', 'install_resource_uninspectable',
                                  'installer_process_uninspectable', 'installer_resource_uninspectable'])
def test_initial_busy_cancel_default_keeps_everything(context, code):
    core = FakeInstaller(context)
    core.failures['recover'] = [CoreError(code)]
    result, core, _, _ = run(context, ['', ''], core=core)
    assert result == 1 and core.calls == ['recover'] and not core.deleted


@pytest.mark.parametrize('outcome', ['rolled_back', 'committed', 'PRIVATE-OUTCOME'])
def test_recovery_precedes_menu_and_never_deletes_previous_backup(context, outcome):
    core = FakeInstaller(context)
    previous = context.root / 'ComfyBridge-backups/previous-transaction'
    core.recovery = SimpleNamespace(recovered=True, outcome=outcome, backup_dir=previous)
    result, core, _, text = run(context, ['0'], core=core)
    assert result == 0 and core.calls == ['recover', 'inspect']
    assert 'previous-transaction' in text
    assert not core.deleted and 'PRIVATE-OUTCOME' not in text
    assert 'retained' in text and '保留' in text


def test_delete_backup_only_exact_success_result_never_previous_recovery(context):
    core = FakeInstaller(context, old_version='0.3.0')
    core.recovery = SimpleNamespace(recovered=True, outcome='rolled_back', backup_dir=context.root / 'old-backup')
    result, core, _, text = run(context, ['2', '', '1', '1', ''], core=core)
    assert result == 0
    assert len(core.deleted) == 1 and core.deleted[0] is core.results[0]
    assert core.deleted[0].transaction_id == 'current-transaction'
    assert 'other backups were not changed' in text


@pytest.mark.parametrize('ending', [[], [''], ['n'], ['0'], ['invalid', '0']])
def test_post_success_eof_and_no_default_keep_backup(context, ending):
    result, core, _, text = run(context, ['1', '1'] + ending)
    assert result == 0 and len(core.results) == 1 and not core.deleted
    assert 'This backup was retained' in text


def test_success_without_backup_skips_delete_question(context):
    result, core, _, text = run(context, ['1', '1', ''], core=FakeInstaller(context, backup=False))
    assert result == 0 and not core.deleted
    assert 'There is no backup to delete' in text
    assert 'Delete only the backup' not in text


def test_backup_deletion_failure_never_changes_install_success_or_echoes_detail(context):
    core = FakeInstaller(context)
    core.failures['delete_backup'] = [CoreError('PRIVATE-CODE')]
    result, core, _, text = run(context, ['1', '1', '1', ''], core=core)
    assert result == 0 and len(core.results) == 1
    assert 'deletion of this backup was not confirmed' in text
    assert 'PRIVATE' not in text


def test_non_successful_selftest_never_announces_success_or_deletes_backup(context):
    core = FakeInstaller(context)
    core.selftest_passed = False
    result, core, _, text = run(context, ['1', '1', ''], core=core)
    assert result == 1 and not core.deleted
    assert 'Local self-test failed' in text
    assert 'File installation and local self-test passed' not in text


@pytest.mark.parametrize('code,message', [('installer_selftest_failed', 'Local self-test failed'),
                                          ('installer_rollback_failed', 'Recovery did not complete'),
                                          ('installer_payload_invalid', 'payload/manifest is invalid'),
                                          ('PRIVATE-UNKNOWN-CODE', 'Installation operation did not complete')])
def test_core_errors_are_actionable_or_generic_but_never_raw(context, code, message):
    core = FakeInstaller(context)
    core.failures['execute'] = [CoreError(code)]
    result, core, _, text = run(context, ['1', '1', ''], core=core)
    assert result == 1 and not core.deleted
    assert message in text
    assert 'PRIVATE' not in text


def test_unknown_warning_and_version_strings_do_not_leak(context):
    core = FakeInstaller(context, new_version='PRIVATE-VERSION')
    core.warnings = ('PRIVATE-WARNING', 'legacy_inventory_untrusted', 'legacy_retired_preserved',
                     'modified_managed', 'preserved_modified_retired', 'external_data_backup_only')
    result, _, _, text = run(context, ['1', '', '0'], core=core)
    assert result == 0 and 'PRIVATE' not in text
    assert 'legacy installation lacks' in text
    assert 'backup scope' in text


def test_modified_managed_list_is_terminal_safe_and_bounded(context):
    core = FakeInstaller(context)
    core.modified = tuple(['\x1b[31m[red]file\x00.dll'] * 101)
    result, _, _, text = run(context, ['1', '', '0'], core=core)
    assert result == 0
    assert '\x1b' not in text and '\x00' not in text
    assert text.count('[red]file.dll') == 100
    assert '1 more entries' in text


@pytest.mark.parametrize('piped', [b'0\n', b'0', b''])
def test_actual_consoleinput_redirected_setup_child_closes_cleanly(context, tmp_path, piped):
    code = '''from pathlib import Path
from types import SimpleNamespace as S
import sys
from comfybridge.setup_launcher import SetupContext, main
root=Path(sys.argv[1])
class Fake:
    def recover(self): return S(recovered=False,outcome='none',backup_dir=None)
    def inspect(self): return S(installed_version=None,target_version='0.4.0',modes=('install',))
context=SetupContext(root,root/'payload.zip',root/'manifest.json',root/'Setup.exe')
raise SystemExit(main([],context=context,installer_factory=lambda _:Fake()))
'''
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    environment = dict(os.environ, PYTHONPATH=str(Path(setup.__file__).resolve().parents[1]), PYTHONIOENCODING='utf-8')
    child = subprocess.run([sys.executable, '-c', code, str(context.root)], cwd=unrelated,
                           input=piped, capture_output=True, env=environment, timeout=8)
    assert child.returncode == (1 if not piped else 0), child.stderr
    assert 'ComfyBridge 离线安装程序'.encode() in child.stdout
    assert b'ComfyBridge Offline Setup' in child.stdout
    assert not (context.root / 'ui.json').exists()


def test_progress_counters_throttled_by_stage_and_ten_percent(context):
    lines = []
    console = setup.SetupConsole(context, FakeInstaller(context), reader=ScriptedReader([]), output=lines.append)
    for completed in range(1001):
        console.progress('backup', completed, 1000)
    assert len(lines) == 11
    assert lines[0].endswith('[0/1000]')
    assert lines[-1].endswith('[1000/1000]')
    console.progress('apply', 0, 1000)
    assert len(lines) == 12 and 'Replacing managed files' in lines[-1]
    console.progress('apply', 1, 1000)
    assert len(lines) == 12
    console.progress('apply', 100, 1000)
    assert len(lines) == 13 and lines[-1].endswith('[100/1000]')


@pytest.mark.parametrize('stage,completed,total', [
    ('PRIVATE-STAGE', 1, 1), ({'private': 'text'}, 1, 1),
    ('backup', 'PRIVATE-COUNT', 10), ('backup', 1, 'PRIVATE-TOTAL'),
    ('backup', True, 10), ('backup', 1, True), ('backup', -1, 10),
    ('backup', 11, 10), ('backup', 1, -1), ('backup', 1.5, 10),
    ('backup', 1, 1_000_000_001),
])
def test_progress_unsafe_values_are_ignored(context, stage, completed, total):
    lines = []
    console = setup.SetupConsole(context, FakeInstaller(context), reader=ScriptedReader([]), output=lines.append)
    console.progress(stage, completed, total)
    assert lines == []


@pytest.mark.parametrize('code,expected', [
    ('installer_disk_space', 'lacks free space'),
    ('installer_unknown_collision', 'collides with an existing unknown file'),
    ('installer_path_collision', 'collides with an existing unknown file'),
    ('installer_backup_collision', 'backup destination already exists'),
    ('installer_path_invalid', 'path is unsafe/unsupported'),
    ('installer_path_unsafe', 'path is unsafe/unsupported'),
    ('installer_volume_unsupported', 'path is unsafe/unsupported'),
    ('installer_install_not_empty', 'installation directory is not empty'),
    ('installer_data_missing', 'external data directory is missing'),
    ('installer_acl_failed', 'Private current-user permissions'),
    ('installer_backup_verify_failed', 'File or backup verification failed'),
    ('installer_source_changed', 'contents changed after confirmation'),
])
def test_stable_core_errors_show_safe_action_and_selected_paths(context, code, expected):
    core = FakeInstaller(context)
    core.failures['execute'] = [CoreError(code)]
    result, _, _, text = run(context, ['1', '1', ''], core=core)
    assert result == 1
    assert expected in text and code in text
    assert str(context.root) in text
    assert str(context.root / 'data') in text
    assert str(core.plans[0].backup_dir) in text
    assert 'PRIVATE-CONFIG-KEY' not in text and 'PRIVATE-EXCEPTION' not in text


def test_actual_core_dataclass_aliases_fit_setup_ui(context):
    from comfybridge.installer_core import Inspection, InstallPlan, InstallResult, RecoveryResult
    core = FakeInstaller(context, old_version='0.3.0')
    core.recovery = RecoveryResult(True, 'rolled_back', context.root / 'prior-backup')
    original_plan = core.plan
    def plan(mode, data_dir=None):
        mock = original_plan(mode, data_dir)
        actual = InstallPlan(root=mock.root, mode=mock.mode, current_version=mock.old_version,
                             target_version=mock.new_version, data_dir=mock.data_dir, backup_path=mock.backup_dir,
                             modified_managed=mock.managed_modified, warnings=(), transaction_id=mock.transaction_id,
                             repair=False, _fingerprint='test-fingerprint', _owner='test-owner')
        core.plans[-1] = actual
        return actual
    def inspect():
        core.called('inspect')
        return Inspection(context.root, '0.4.0', '0.3.0', ('update',), installed=True)
    def execute(plan, progress=None):
        core.called('execute')
        actual = InstallResult(plan.root, '0.4.0', plan.mode, plan.data_dir, plan.backup_path, plan.transaction_id)
        core.results.append(actual)
        return actual
    core.plan, core.inspect, core.execute = plan, inspect, execute
    result, _, _, text = run(context, ['2', '', '1', '0', ''], core=core)
    assert result == 0
    assert 'rolled back' in text and '0.3.0' in text and '0.4.0' in text
    assert not core.deleted


def test_separate_script_entry_delegates_to_setup_main(monkeypatch):
    import runpy
    calls = []
    monkeypatch.setattr(setup, 'main', lambda: calls.append('setup') or 0)
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/setup_entry.py'), run_name='__main__')
    assert stopped.value.code == 0 and calls == ['setup']


@pytest.mark.parametrize('piped,delete', [(b'2\n\n1\n0\n\n', False), (b'2\n\n1\n1\n\n', True),
                                         (b'2\n\n1\n', False)])
def test_actual_redirected_update_consumes_confirm_delete_pause_without_prefetch(context, tmp_path, piped, delete):
    code = '''from pathlib import Path
from types import SimpleNamespace as S
import sys
from comfybridge.setup_launcher import SetupContext, main
root=Path(sys.argv[1])
class Fake:
    def recover(self): return S(recovered=False,outcome='none',backup_dir=None)
    def inspect(self): return S(installed_version='0.3.0',target_version='0.4.0',modes=('update',))
    def plan(self,mode,data_dir=None):
        assert mode=='update' and data_dir==root/'data'
        return S(root=root,mode=mode,old_version='0.3.0',new_version='0.4.0',data_dir=data_dir,backup_dir=root/'backup/current',managed_modified=(),warnings=())
    def execute(self,plan,progress=None):
        print('OWN_EXECUTE_ONCE')
        return S(backup_dir=plan.backup_dir,selftest_passed=True)
    def delete_backup(self,result): print('OWN_DELETE_CURRENT_ONLY')
context=SetupContext(root,root/'payload.zip',root/'manifest.json',root/'Setup.exe')
raise SystemExit(main([],context=context,installer_factory=lambda _:Fake()))
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(setup.__file__).resolve().parents[1]), PYTHONIOENCODING='utf-8')
    child = subprocess.run([sys.executable, '-c', code, str(context.root)], cwd=tmp_path,
                           input=piped, capture_output=True, env=environment, timeout=8)
    assert child.returncode == 0, child.stderr
    assert child.stdout.count(b'OWN_EXECUTE_ONCE') == 1
    assert (b'OWN_DELETE_CURRENT_ONLY' in child.stdout) is delete
    assert b'Press Enter to close' in child.stdout


@pytest.mark.parametrize('interactive', [True, False])
def test_stdio_configuration_preserves_console_and_configures_only_redirected_streams(monkeypatch, interactive):
    import io
    class Stream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.configurations = []
        def isatty(self):
            return interactive
        def reconfigure(self, **values):
            self.configurations.append(values)
    stdout, stderr = Stream(), Stream()
    monkeypatch.setattr(sys, 'stdout', stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)
    setup._configure_stdio()
    expected = [] if interactive else [{'encoding': 'utf-8', 'errors': 'replace', 'line_buffering': True}]
    assert stdout.configurations == expected
    assert stderr.configurations == expected


def test_stdio_configuration_accepts_unsupported_capture_streams(monkeypatch):
    import io
    class ClosedLike(io.StringIO):
        def isatty(self):
            raise ValueError('closed stream')
    monkeypatch.setattr(sys, 'stdout', ClosedLike())
    monkeypatch.setattr(sys, 'stderr', None)
    setup._configure_stdio()


@pytest.mark.parametrize('action', ['menu', 'help', 'argument-error', 'external-update'])
def test_actual_regular_file_redirection_forces_utf8_before_any_setup_output(context, tmp_path, action):
    external = tmp_path / '额外 私密数据'
    external.mkdir()
    code = '''from pathlib import Path
from types import SimpleNamespace as S
import sys
from comfybridge.setup_launcher import SetupContext, main
assert sys.stdout.encoding.lower() in ('cp936','gbk')
assert sys.stderr.encoding.lower() in ('cp936','gbk')
root, external=Path(sys.argv[1]),Path(sys.argv[3])
class Fake:
    def recover(self): return S(recovered=False,outcome='none',backup_dir=None)
    def inspect(self): return S(installed_version='0.3.0',target_version='0.4.0',modes=('update',))
    def plan(self,mode,data_dir=None):
        assert mode=='update' and data_dir==external
        return S(root=root,mode=mode,old_version='0.3.0',new_version='0.4.0',data_dir=data_dir,backup_dir=None,managed_modified=(),warnings=())
    def execute(self,plan,progress=None): return S(backup_dir=None,selftest_passed=True)
    def delete_backup(self,result): raise AssertionError('not applicable')
context=SetupContext(root,root/'payload.zip',root/'manifest.json',root/'Setup.exe')
action=sys.argv[2]
arguments=['--help'] if action=='help' else (['--target','PRIVATE-ARGUMENT'] if action=='argument-error' else [])
result=main(arguments,context=context,installer_factory=lambda _:Fake())
print('标准错误中文校验',file=sys.stderr)
raise SystemExit(result)
'''
    environment = dict(os.environ, PYTHONPATH=str(Path(setup.__file__).resolve().parents[1]),
                       PYTHONIOENCODING='cp936', PYTHONUTF8='0')
    input_file, output_file, error_file = (tmp_path / name for name in ('stdin.txt', 'stdout.txt', 'stderr.txt'))
    transcript = f'2\n2\n{external}\n1\n\n' if action == 'external-update' else '0\n'
    input_file.write_bytes(transcript.encode('utf-8'))
    with input_file.open('rb') as stdin, output_file.open('wb') as stdout, error_file.open('wb') as stderr:
        child = subprocess.run([sys.executable, '-c', code, str(context.root), action, str(external)],
                               cwd=tmp_path, stdin=stdin, stdout=stdout, stderr=stderr,
                               env=environment, timeout=8)
    text, errors = output_file.read_bytes().decode('utf-8'), error_file.read_bytes().decode('utf-8')
    assert child.returncode == (2 if action == 'argument-error' else 0), errors
    assert '标准错误中文校验' in errors
    assert 'PRIVATE-ARGUMENT' not in text
    if action == 'help':
        assert '把 Setup 放到目标目录后运行' in text
    elif action == 'argument-error':
        assert '不接受命令行安装位置' in text
    else:
        assert 'ComfyBridge 离线安装程序' in text and str(context.root) in text
    if action == 'external-update':
        assert str(external) in text and '文件安装及本地自检已通过' in text
