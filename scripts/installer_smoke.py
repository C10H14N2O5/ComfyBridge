"""Exercise the real offline Setup EXE in owned isolated directories, never production data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): digest(path)
            for path in root.rglob('*') if path.is_file() and path.name != '.comfybridge-data.lease'}


def run_setup(executable: Path, commands: str, cwd: Path, env: dict, label: str,
              reports: Path, *, language: str = '1') -> subprocess.CompletedProcess:
    # A onefile EXE has a bootstrap parent and worker. A plain subprocess timeout
    # kills only the parent, possibly leaving a worker and its inherited pipes alive.
    # Start suspended, assign our exact child tree to a private kill-on-close job,
    # then resume. This test-only containment never enumerates/kills user processes.
    import msvcrt
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    stdout_path = reports / (label + '.stdout.txt')
    stderr_path = reports / (label + '.stderr.txt')
    started = time.monotonic()
    process = thread = job = None
    with tempfile.TemporaryFile() as incoming, stdout_path.open('wb') as outgoing, stderr_path.open('wb') as errors:
        # Setup now requires an explicit session-language choice first.
        incoming.write((language + '\n' + commands).encode('utf-8'))
        incoming.seek(0)
        handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in (incoming, outgoing, errors)]
        for handle in handles:
            os.set_handle_inheritable(handle, True)
        try:
            info = win32process.STARTUPINFO()
            info.dwFlags = win32con.STARTF_USESTDHANDLES
            info.hStdInput, info.hStdOutput, info.hStdError = handles
            job = win32job.CreateJobObject(None, '')
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            process, thread, pid, tid = win32process.CreateProcess(
                str(executable), subprocess.list2cmdline([str(executable)]), None, None, True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_UNICODE_ENVIRONMENT, env, str(cwd), info)
            win32job.AssignProcessToJobObject(job, process)
            win32process.ResumeThread(thread)
            if win32event.WaitForSingleObject(process, 180000) != win32event.WAIT_OBJECT_0:
                win32job.TerminateJobObject(job, 71)
                win32event.WaitForSingleObject(process, 10000)
                raise subprocess.TimeoutExpired([str(executable)], 180)
            code = win32process.GetExitCodeProcess(process)
        finally:
            # If assignment/resume failed, terminate only our suspended child.
            if process is not None and win32process.GetExitCodeProcess(process) == 259:
                win32api.TerminateProcess(process, 71)
            for handle in (thread, process, job):
                if handle is not None:
                    handle.Close()
            for handle in handles:
                os.set_handle_inheritable(handle, False)
    (reports / (label + '.timing.json')).write_text(
        json.dumps({'seconds': round(time.monotonic() - started, 3), 'exit_code': code}) + '\n', encoding='utf-8')
    return subprocess.CompletedProcess([str(executable)], code, stdout_path.read_bytes(), stderr_path.read_bytes())


def put_setup(source: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / 'Setup.exe'
    shutil.copyfile(source, target)
    return target


def assert_installed(directory: Path, release: Path) -> None:
    for relative, expected in snapshot(release).items():
        installed = directory / relative
        assert installed.is_file() and digest(installed) == expected, relative
    assert (directory / '.comfybridge-install/receipt.json').is_file()
    assert not (directory / '.comfybridge-install/transaction.json').exists()


def backup_names(directory: Path) -> set[str]:
    backups = directory / 'backup'
    return {path.name for path in backups.iterdir() if path.is_dir()} if backups.exists() else set()


def assert_one_new_backup(directory: Path, previous: set[str]) -> None:
    """An upgrade adds one backup without removing backups copied from the old release."""
    current = backup_names(directory)
    assert previous <= current, 'legacy_backup_removed'
    assert len(current - previous) == 1, 'legacy_expected_one_new_backup'


def check_open_configurator(directory: Path, setup: Path, case: Path, reports: Path,
                            env: dict, elsewhere: Path, label: str) -> None:
    from packaged_smoke import cache_config
    from comfybridge.config import save_config
    from comfybridge.ui_preferences import UIPreferences, save_ui_preferences

    data = case / (label + '-editor-data')
    data.mkdir()
    config = cache_config(data)
    config.comfyui.base_url = 'http://127.0.0.1:9'
    save_config(data, config)
    save_ui_preferences(data, UIPreferences(language='en-US'))
    log = reports / (label + '-editor.txt')
    receipt = directory / '.comfybridge-install/receipt.json'
    old_receipt = digest(receipt) if receipt.exists() else None
    old_backups = backup_names(directory)
    with log.open('wb') as output:
        editor = subprocess.Popen([str(directory / 'ComfyBridge.exe'), '--data-dir', str(data), 'configure'],
                                  stdin=subprocess.PIPE, stdout=output, stderr=output, env=env)
        try:
            until = time.monotonic() + 15
            while time.monotonic() < until and log.stat().st_size == 0 and editor.poll() is None:
                time.sleep(.05)
            assert editor.poll() is None and log.stat().st_size > 0, 'editor_not_waiting'
            result = run_setup(setup, '2\n\n1\n0\n\n', elsewhere, env, label + '-busy', reports)
            text = result.stdout.decode('utf-8')
            assert '重新检查' in text, 'busy_not_reported'
            assert backup_names(directory) == old_backups, 'busy_update_created_backup'
            assert (digest(receipt) if receipt.exists() else None) == old_receipt
        finally:
            try:
                editor.communicate(b'q\n', timeout=10)
            except subprocess.TimeoutExpired:
                # Only this harness-owned configurator, never a backend or user process.
                editor.kill()
                editor.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('setup', type=Path)
    parser.add_argument('release', type=Path)
    parser.add_argument('--legacy-release', type=Path)
    args = parser.parse_args()
    setup = args.setup.resolve(strict=True)
    release = args.release.resolve(strict=True)
    case = ROOT / 'artifacts' / ('installer-smoke-' + uuid.uuid4().hex)
    reports = case / 'reports'
    reports.mkdir(parents=True)
    elsewhere = case / 'unrelated working directory'
    elsewhere.mkdir()
    env = dict(os.environ)
    env['PATH'] = str(Path(env.get('SystemRoot', r'C:\Windows')) / 'System32')
    env.pop('VIRTUAL_ENV', None)
    env.pop('PYTHONPATH', None)
    report = {'schema_version': 1, 'setup_sha256': digest(setup), 'release': release.name,
              'case': case.name, 'isolated_path': True, 'real_gpu_requests': 0}
    # Keep evidence on failure; do not recursively clean any user paths.
    fresh = case / '中文 全新安装'
    fresh_setup = put_setup(setup, fresh)
    result = run_setup(fresh_setup, '1\n1\n0\n\n', elsewhere, env, 'install', reports)
    assert result.returncode == 0, 'fresh_install_failed'
    assert_installed(fresh, release)
    assert (fresh / 'data').is_dir() and not (fresh / 'data/config.json').exists()
    assert not (elsewhere / 'ComfyBridge.exe').exists()
    report['fresh_install_actual_exe_parent'] = True
    selftest = subprocess.run([str(fresh / 'ComfyBridge.exe'), '--installation-self-test'],
                              capture_output=True, env=env, cwd=elsewhere, timeout=30)
    assert selftest.returncode == 0
    diagnostic = json.loads(selftest.stdout)
    from comfybridge import __version__
    assert diagnostic['ok'] is True and diagnostic['version'] == __version__
    report['selftest'] = diagnostic
    # Verify the real onefile Setup follows its English selection as well.
    english = case / 'English 全新安装'
    english_setup = put_setup(setup, english)
    english_result = run_setup(english_setup, '1\n1\n0\n\n', elsewhere, env, 'install-english', reports, language='2')
    assert english_result.returncode == 0, 'english_install_failed'
    english_text = english_result.stdout.decode('utf-8').replace('\r\n', '\n')
    assert '\n[1] Install\n' in english_text and '\n[1] 安装\n' not in english_text, 'setup_language_not_applied'
    assert 'File installation and local self-test passed' in english_text
    assert not (english / 'data/ui.json').exists(), 'setup_persisted_language'
    assert_installed(english, release)
    report['english_setup_install_and_session_only_language'] = True
    check_open_configurator(fresh, fresh_setup, case, reports, env, elsewhere, 'current')
    report['current_open_configurator_blocks_update'] = True
    sentinel = fresh / 'my-notes.txt'
    sentinel.write_text('unknown user file must survive\n', encoding='utf-8')
    (fresh / 'docs/my-notes.txt').write_text('nested unknown must survive\n', encoding='utf-8')
    data = fresh / 'data'
    (data / 'private-example.txt').write_text('synthetic-test-key-only\n', encoding='utf-8')
    before_data = snapshot(data)
    old_backups = backup_names(fresh)
    # Known program file modifications are explicitly repaired; unknown files remain.
    (fresh / 'README.md').write_text('simulated damaged program document\n', encoding='utf-8')
    result = run_setup(fresh_setup, '2\n\n1\n0\n\n', elsewhere, env, 'repair-keep', reports)
    assert result.returncode == 0, 'same_version_repair_failed'
    assert_installed(fresh, release)
    assert sentinel.read_text(encoding='utf-8').startswith('unknown user file')
    assert (fresh / 'docs/my-notes.txt').read_text(encoding='utf-8').startswith('nested unknown')
    assert snapshot(data) == before_data
    retained = backup_names(fresh)
    assert len(retained - old_backups) == 1, 'repair_backup_not_retained'
    report['same_version_repair_unknown_files_and_data_preserved'] = True
    report['backup_retained_by_default'] = True
    result = run_setup(fresh_setup, '2\n\n1\n1\n\n', elsewhere, env, 'repair-delete-own', reports)
    assert result.returncode == 0, 'repair_delete_failed'
    assert backup_names(fresh) == retained, 'deleted_other_backup_or_failed_to_delete_own'
    assert snapshot(data) == before_data
    assert_installed(fresh, release)
    report['delete_only_current_backup'] = True
    # Actual installed code executes existing TCP/SDK/audio/lifecycle fake-server suite.
    subprocess.run([sys.executable, str(ROOT / 'scripts/packaged_smoke.py'), str(fresh)], check=True)
    business = json.loads((ROOT / 'artifacts/packaged-smoke.json').read_text(encoding='utf-8'))
    (reports / 'installed-business.json').write_text(json.dumps(business, indent=2) + '\n', encoding='utf-8')
    report['installed_business_smoke'] = business
    if args.legacy_release:
        legacy_source = args.legacy_release.resolve(strict=True)
        legacy = case / '中文 旧版本升级'
        shutil.copytree(legacy_source, legacy)
        legacy_setup = put_setup(setup, legacy)
        check_open_configurator(legacy, legacy_setup, case, reports, env, elsewhere, 'legacy')
        report['legacy_open_configurator_blocks_update'] = True
        external = case / '外部 data'
        external.mkdir()
        (external / 'config.json').write_text('{"synthetic":"preserve-without-parsing"}\n', encoding='utf-8')
        (external / 'runtime').mkdir()
        for name in ('tasks.sqlite3', 'tasks.sqlite3-wal', 'tasks.sqlite3-shm'):
            (external / 'runtime' / name).write_bytes(b'synthetic backup fixture, not an active SQLite database')
        (legacy / 'extra-user-file.txt').write_text('preserve\n', encoding='utf-8')
        before = snapshot(external)
        old_legacy_backups = backup_names(legacy)
        old_backup_contents = snapshot(legacy / 'backup') if old_legacy_backups else {}
        result = run_setup(legacy_setup, '2\n2\n' + str(external) + '\n1\n0\n\n',
                           elsewhere, env, 'legacy-update-external', reports)
        assert result.returncode == 0, 'legacy_update_failed'
        assert_installed(legacy, release)
        assert snapshot(external) == before
        assert (legacy / 'extra-user-file.txt').read_text(encoding='utf-8') == 'preserve\n'
        assert_one_new_backup(legacy, old_legacy_backups)
        current_backup_contents = snapshot(legacy / 'backup')
        assert all(current_backup_contents.get(path) == checksum for path, checksum in old_backup_contents.items()), 'legacy_backup_content_changed'
        report['legacy_existing_backups_preserved'] = True
        report['legacy_original_release'] = json.loads((legacy_source / 'BUILD-INFO.json').read_text(encoding='utf-8-sig'))['version']
        report['legacy_upgrade_external_data_unchanged'] = True
    # Never alter a non-product directory on mistaken Update/Install.
    foreign = case / 'not a product'
    foreign_setup = put_setup(setup, foreign)
    (foreign / 'valuable.txt').write_text('do not overwrite\n', encoding='utf-8')
    before = snapshot(foreign)
    run_setup(foreign_setup, '1\n1\n\n', elsewhere, env, 'foreign-install-refused', reports)
    assert (foreign / 'valuable.txt').read_text(encoding='utf-8') == 'do not overwrite\n'
    assert not (foreign / 'ComfyBridge.exe').exists()
    assert before['Setup.exe'] == digest(foreign_setup)
    report['foreign_directory_refused'] = True
    report['completed'] = True
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    (reports / 'result.json').write_text(text, encoding='utf-8')
    (ROOT / 'artifacts/installer-smoke.json').write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
