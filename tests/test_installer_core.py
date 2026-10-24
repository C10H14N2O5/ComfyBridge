"""No executable/network/business start. Faults cover every durable rename boundary."""
import json
import os
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from comfybridge import installer_core as core
from comfybridge.installer_core import Installer, InstallerError
from comfybridge.installer_manifest import build_manifest, sha256_file


class Crash(BaseException):
    pass


class Guard:
    def __init__(self, root, data, managed_files=(), **kwargs):
        self.root, self.data = root, data
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def recheck(self, managed_files=None): pass


@pytest.fixture(autouse=True)
def isolated_platform(monkeypatch):
    # Path/reparse/hardlink and private ACL helpers remain real. Occupancy and
    # executable testing are independent platform integration responsibilities.
    monkeypatch.setattr(core.platform, 'validate_local_ntfs', core.platform.assert_safe_path)
    monkeypatch.setattr(core.platform, 'SetupGuard', Guard)
    monkeypatch.setattr(Installer, '_self_test', lambda *_: None)


def make_payload(tmp_path, version='0.4.0', extra=None):
    release = tmp_path / ('payload-' + uuid.uuid4().hex)
    release.mkdir()
    files = {'ComfyBridge.exe': b'fake executable ' + version.encode(),
             'BUILD-INFO.json': json.dumps({'product': 'ComfyBridge', 'version': version}).encode(),
             '_internal/library.dll': b'unchanged library', 'README.md': b'unchanged readme'}
    files.update(extra or {})
    for name, content in files.items():
        path = release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    archive = release.with_suffix('.zip')
    return archive, build_manifest(release, archive)


def make_legacy(root, version='0.3.0'):
    files = {'ComfyBridge.exe': b'old executable',
             'BUILD-INFO.json': json.dumps({'product': 'ComfyBridge', 'version': version}).encode(),
             '_internal/library.dll': b'old library', 'README.md': b'old readme',
             'docs/obsolete.txt': b'legacy original retired'}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    sums = ''.join(f'{sha256_file(root / name)}  {name}\n' for name in files)
    (root / 'SHA256SUMS.txt').write_text(sums)
    return files


def instance(tmp_path, version='0.4.0', legacy=False):
    root = tmp_path / 'install'
    root.mkdir()
    if legacy: make_legacy(root)
    archive, document = make_payload(tmp_path, version)
    return Installer(root, archive, document), document


def data_tree(root):
    data = root / 'data'
    (data / 'runtime').mkdir(parents=True)
    (data / 'empty').mkdir()
    for name, content in {'config.json': b'{"api_key":"secret-never-log"}', 'ui.json': b'language',
                          'runtime/tasks.sqlite3': b'database', 'runtime/tasks.sqlite3-wal': b'wal',
                          'runtime/tasks.sqlite3-shm': b'shm', '.hidden': b'hidden'}.items():
        (data / name).write_bytes(content)
    return data


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob('*')
            if path.is_file() and path.name != '.comfybridge-data.lease'}


def old_snapshot(root):
    return {name: content for name, content in snapshot(root).items()
            if name.split('/')[0] not in (core.STATE, core.BACKUPS)}


def test_fresh_install_and_probe_does_not_create_data(tmp_path):
    installer, document = instance(tmp_path)
    assert installer.inspect().modes == ('install',)
    plan = installer.plan('install')
    assert not (installer.root / 'data').exists()
    assert plan.backup_dir == installer.root / 'backup' / plan.transaction_id
    stages = []
    result = installer.execute(plan, lambda stage, done, total: stages.append(stage))
    assert result.selftest_passed and result.backup_path.exists()
    assert not installer.marker.exists()
    assert installer.inspect().modes == ('repair',)
    assert json.loads(installer.receipt_path.read_bytes())['manifest'] == document
    assert 'commit' in stages


@pytest.mark.parametrize('version', ['0.1.0', '0.2.0', '0.3.0'])
def test_legacy_upgrade_full_backup_preserves_data_unknown_and_retired(tmp_path, version):
    installer, _ = instance(tmp_path)
    make_legacy(installer.root, version)
    data = data_tree(installer.root)
    (installer.root / 'unknown.txt').write_bytes(b'private unknown file')
    (installer.root / 'unknown-empty').mkdir()
    before = old_snapshot(installer.root)
    data_before = snapshot(data)
    plan = installer.plan('update')
    assert 'legacy_inventory_untrusted' in plan.warnings
    result = installer.execute(plan)
    assert snapshot(data) == data_before
    assert (installer.root / 'unknown.txt').read_bytes() == b'private unknown file'
    assert (installer.root / 'docs/obsolete.txt').read_bytes() == b'legacy original retired'
    backed = snapshot(result.backup_dir / 'program')
    assert backed == {name: value for name, value in before.items() if not name.startswith('data/')}
    assert snapshot(result.backup_dir / 'data') == data_before
    assert (result.backup_dir / 'program/unknown-empty').is_dir()
    assert (result.backup_dir / 'data/empty').is_dir()
    for item in result.backup_dir.rglob('*'):
        assert core.platform.verify_private(item)


def test_external_data_backup_also_copies_unselected_root_data(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    local = data_tree(installer.root)
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'secret.json').write_bytes(b'external secret')
    before_local, before_external = snapshot(local), snapshot(external)
    plan = installer.plan('update', external)
    assert 'external_data_backup_only' in plan.warnings
    result = installer.execute(plan)
    assert snapshot(result.backup_dir / 'program/data') == before_local
    assert snapshot(result.backup_dir / 'data') == before_external
    assert snapshot(local) == before_local and snapshot(external) == before_external


def test_only_own_result_backup_can_be_deleted(tmp_path):
    installer, _ = instance(tmp_path)
    first = installer.execute(installer.plan('install'))
    second = installer.execute(installer.plan('repair'))
    with pytest.raises(InstallerError, match='installer_backup_identity'):
        installer.delete_backup(replace(second))
    installer.delete_backup(second)
    assert first.backup_path.exists() and not second.backup_path.exists()
    with pytest.raises(InstallerError): installer.delete_backup(second)


def test_backup_carries_previous_receipt_for_manual_program_recovery(tmp_path):
    installer, _ = instance(tmp_path)
    installer.execute(installer.plan('install'))
    previous = json.loads(installer.receipt_path.read_bytes())
    result = installer.execute(installer.plan('repair'))
    metadata = json.loads((result.backup_dir / 'backup.json').read_bytes())
    assert metadata['previous_receipt'] == previous and metadata['verified'] is True


@pytest.mark.parametrize('damage', ['missing', 'garbage', 'changed_exe', 'missing_exe'])
def test_receipt_owned_damage_repairable(tmp_path, damage):
    installer, _ = instance(tmp_path)
    installer.execute(installer.plan('install'))
    if damage == 'missing': (installer.root / 'BUILD-INFO.json').unlink()
    elif damage == 'garbage': (installer.root / 'BUILD-INFO.json').write_bytes(b'not json')
    elif damage == 'missing_exe': (installer.root / 'ComfyBridge.exe').unlink()
    else: (installer.root / 'ComfyBridge.exe').write_bytes(b'locally modified')
    summary = installer.inspect()
    assert summary.modes == ('repair',) and summary.managed_modified
    installer.execute(installer.plan('repair'))
    assert not installer.inspect().managed_modified


def test_conflicting_product_and_downgrade_rejected(tmp_path):
    installer, _ = instance(tmp_path, '0.5.0')
    installer.execute(installer.plan('install'))
    archive, manifest = make_payload(tmp_path, '0.4.0')
    older = Installer(installer.root, archive, manifest)
    assert older.inspect().modes == ()
    with pytest.raises(InstallerError, match='installer_downgrade'): older.plan('update')
    (installer.root / 'BUILD-INFO.json').write_text('{"product":"Other","version":"0.5.0"}')
    with pytest.raises(InstallerError, match='installer_product_invalid'): installer.inspect()


@pytest.mark.parametrize('unknown', ['empty-dir', 'data/file', '.comfybridge-install/unrelated', 'other.txt'])
def test_nonempty_or_unrelated_fresh_root_rejected(tmp_path, unknown):
    installer, _ = instance(tmp_path)
    target = installer.root / unknown
    target.parent.mkdir(parents=True, exist_ok=True)
    if unknown == 'empty-dir': target.mkdir()
    else: target.write_bytes(b'not product')
    with pytest.raises(InstallerError, match='installer_install_not_empty'): installer.plan('install')


def test_unknown_collision_and_untrusted_legacy_retired_preserved(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    (installer.root / 'README.md').write_bytes(b'user-managed modification')
    plan = installer.plan('update')
    assert 'README.md' in plan.managed_modified
    # Remove a common path from inventory: existing target is then UNKNOWN.
    sums = installer.root / 'SHA256SUMS.txt'
    sums.write_text('\n'.join(line for line in sums.read_text().splitlines() if not line.endswith('  README.md')))
    with pytest.raises(InstallerError, match='installer_unknown_collision'): installer.plan('update')


def test_legacy_shared_third_party_source_is_known_product_inventory(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    relative = 'third-party-sources/pinned-source.tar.gz'
    source = installer.root / relative
    source.parent.mkdir()
    source.write_bytes(b'original source archive')
    with (installer.root / 'SHA256SUMS.txt').open('a') as sums:
        sums.write(f'{sha256_file(source)}  {relative}\n')
    archive, manifest = make_payload(tmp_path, extra={relative: b'new pinned source archive'})
    installer = Installer(installer.root, archive, manifest)
    result = installer.execute(installer.plan('update'))
    assert source.read_bytes() == b'new pinned source archive'
    assert (result.backup_dir / 'program' / relative).read_bytes() == b'original source archive'


def test_plan_source_mutation_rejected_and_forged_plan_rejected(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    plan = installer.plan('update')
    with pytest.raises(InstallerError, match='installer_plan_invalid'): installer.execute(replace(plan))
    (installer.root / 'README.md').write_bytes(b'changed after preview')
    with pytest.raises(InstallerError, match='installer_plan_stale'): installer.execute(plan)
    assert not installer.marker.exists()


def test_diskspace_fails_before_transaction(tmp_path, monkeypatch):
    installer, _ = instance(tmp_path)
    monkeypatch.setattr(core.shutil, 'disk_usage', lambda *_: SimpleNamespace(free=1))
    with pytest.raises(InstallerError, match='installer_disk_space'): installer.plan('install')
    assert not installer.marker.exists()


def test_data_hardlink_refused_without_copy_or_mutation(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    data = data_tree(installer.root)
    os.link(data / 'config.json', data / 'hardlinked')
    with pytest.raises(InstallerError, match='installer_path_unsafe'): installer.plan('update')
    assert not installer.marker.exists()


FAULTS = ['staged', 'staged_selftest', 'backup_file', 'backup_verified', 'before_withdraw',
          'after_withdraw', 'before_publish', 'after_publish', 'installed_selftest',
          'before_receipt', 'after_receipt', 'committed']


@pytest.mark.parametrize('point', FAULTS)
def test_regular_fault_rolls_back_program_never_data(tmp_path, monkeypatch, point):
    installer, _ = instance(tmp_path, legacy=True)
    data = data_tree(installer.root)
    before, before_data = old_snapshot(installer.root), snapshot(data)
    plan = installer.plan('update')
    fired = False
    def fault(actual, path=''):
        nonlocal fired
        if actual == point and not fired:
            fired = True
            raise OSError('fault text must not surface')
    monkeypatch.setattr(installer, '_checkpoint', fault)
    with pytest.raises(InstallerError) as error: installer.execute(plan)
    assert fired and 'fault text' not in str(error.value)
    assert old_snapshot(installer.root) == before and snapshot(data) == before_data
    assert not installer.marker.exists()


@pytest.mark.parametrize('point', FAULTS)
def test_abrupt_fault_new_installer_recovers_without_replay(tmp_path, monkeypatch, point):
    installer, _ = instance(tmp_path, legacy=True)
    data = data_tree(installer.root)
    before, before_data = old_snapshot(installer.root), snapshot(data)
    plan = installer.plan('update')
    def fault(actual, path=''):
        if actual == point: raise Crash()
    monkeypatch.setattr(installer, '_checkpoint', fault)
    with pytest.raises(Crash): installer.execute(plan)
    assert installer.marker.exists()
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    monkeypatch.setattr(fresh, '_self_test', lambda *_: pytest.fail('recovery must not run a program'))
    result = fresh.recover()
    assert result.outcome == ('committed' if point == 'committed' else 'rolled_back')
    if point != 'committed': assert old_snapshot(installer.root) == before
    assert snapshot(data) == before_data and not fresh.marker.exists()
    assert fresh.recover().outcome == 'none'


@pytest.mark.parametrize('ordinal', range(1, 8))
def test_identical_repair_second_crash_during_rollback_idempotent(tmp_path, monkeypatch, ordinal):
    installer, _ = instance(tmp_path)
    installer.execute(installer.plan('install'))
    before = old_snapshot(installer.root)
    old_receipt = installer.receipt_path.read_bytes()
    plan = installer.plan('repair')
    monkeypatch.setattr(installer, '_checkpoint', lambda point, path='': (_ for _ in ()).throw(Crash()) if point == 'after_receipt' else None)
    with pytest.raises(Crash): installer.execute(plan)
    recovery = Installer(installer.root, installer.payload_zip, installer.manifest)
    count = 0
    def fault(point, path=''):
        nonlocal count
        if point == 'rollback_action':
            count += 1
            if count == ordinal: raise Crash()
    monkeypatch.setattr(recovery, '_checkpoint', fault)
    try: recovery.recover()
    except Crash: pass
    final = Installer(installer.root, installer.payload_zip, installer.manifest)
    final.recover()
    assert old_snapshot(installer.root) == before
    assert json.loads(final.receipt_path.read_bytes()) == json.loads(old_receipt)
    assert not final.marker.exists()


def crash_marker(installer, monkeypatch, point='after_publish'):
    plan = installer.plan('update' if installer.inspect().installed else 'install')
    monkeypatch.setattr(installer, '_checkpoint', lambda actual, path='': (_ for _ in ()).throw(Crash()) if actual == point else None)
    with pytest.raises(Crash): installer.execute(plan)
    return json.loads(installer.marker.read_bytes())


def test_recovery_rereads_under_guard_no_stale_action(tmp_path, monkeypatch):
    installer, _ = instance(tmp_path, legacy=True)
    document = crash_marker(installer, monkeypatch)
    before = snapshot(installer.root)
    class ChangedGuard(Guard):
        def __enter__(self):
            changed = dict(document, data_dir=str(tmp_path / 'changed'))
            installer.marker.write_text(json.dumps(changed))
            return self
    monkeypatch.setattr(core.platform, 'SetupGuard', ChangedGuard)
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    with pytest.raises(InstallerError, match='installer_state_changed'): fresh.recover()
    after = snapshot(installer.root)
    before.pop('.comfybridge-install/transaction.json')
    after.pop('.comfybridge-install/transaction.json')
    assert before == after


@pytest.mark.parametrize('corruption', ['type', 'order', 'unsafe_path', 'schema_bool', 'backup_false'])
def test_corrupt_wal_never_mutates_program(tmp_path, monkeypatch, corruption):
    installer, _ = instance(tmp_path, legacy=True)
    document = crash_marker(installer, monkeypatch)
    if corruption == 'type': document['transaction_id'] = None
    elif corruption == 'order': document['actions'] = list(reversed(document['actions']))
    elif corruption == 'unsafe_path': document['actions'][0]['path'] = 'data/config.json'
    elif corruption == 'schema_bool': document['schema_version'] = True
    elif corruption == 'backup_false': document['backup_verified'] = False
    installer.marker.write_text(json.dumps(document))
    before = snapshot(installer.root)
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    with pytest.raises(InstallerError): fresh.recover()
    assert snapshot(installer.root) == before


def test_committed_recovery_receipt_must_match_whole_manifest(tmp_path, monkeypatch):
    installer, _ = instance(tmp_path)
    document = crash_marker(installer, monkeypatch, 'committed')
    receipt = json.loads(installer.receipt_path.read_bytes())
    receipt['manifest']['build_id'] = 'different'
    installer.receipt_path.write_text(json.dumps(receipt))
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    with pytest.raises(InstallerError, match='installer_recovery_failed'): fresh.recover()
    assert fresh.marker.exists()


@pytest.mark.parametrize('point', ['created_directory', 'after_publish'])
def test_fresh_nested_interruption_recovery_allows_retry(tmp_path, monkeypatch, point):
    installer, _ = instance(tmp_path)
    plan = installer.plan('install')
    def fail(actual, path=''):
        if actual == point and path.startswith('_internal'):
            raise Crash()
    monkeypatch.setattr(installer, '_checkpoint', fail)
    with pytest.raises(Crash): installer.execute(plan)
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    fresh.recover()
    assert not (fresh.root / '_internal').exists()
    assert fresh.inspect().modes == ('install',)
    fresh.execute(fresh.plan('install'))
    assert fresh.inspect().modes == ('repair',)


def test_preexisting_empty_directory_survives_rollback(tmp_path, monkeypatch):
    installer, _ = instance(tmp_path, legacy=True)
    (installer.root / 'empty-user-directory').mkdir()
    plan = installer.plan('update')
    monkeypatch.setattr(installer, '_checkpoint', lambda point, path='': (_ for _ in ()).throw(Crash()) if point == 'after_receipt' else None)
    with pytest.raises(Crash): installer.execute(plan)
    fresh = Installer(installer.root, installer.payload_zip, installer.manifest)
    fresh.recover()
    assert (fresh.root / 'empty-user-directory').is_dir()


def test_full_backup_does_not_hide_setup_like_user_files(tmp_path):
    installer, _ = instance(tmp_path, legacy=True)
    for name in ['ComfyBridge-Setup-notes.txt', 'Setup.exe']:
        (installer.root / name).write_bytes(b'user-owned')
    result = installer.execute(installer.plan('update'))
    for name in ['ComfyBridge-Setup-notes.txt', 'Setup.exe']:
        assert (result.backup_dir / 'program' / name).read_bytes() == b'user-owned'
        assert (installer.root / name).read_bytes() == b'user-owned'


def test_transaction_scans_not_per_file_quadratic(tmp_path, monkeypatch):
    installer, _ = instance(tmp_path, legacy=True)
    calls = []
    class CountingGuard(Guard):
        def recheck(self, managed_files=None): calls.append(managed_files)
    monkeypatch.setattr(core.platform, 'SetupGuard', CountingGuard)
    installer.execute(installer.plan('update'))
    assert len(calls) == 2  # Before apply, then after launcher withdrawal.


def test_modified_receipt_retired_file_preserved_original_retired_removed(tmp_path):
    installer, _ = instance(tmp_path)
    archive, manifest = make_payload(tmp_path, '0.4.0', {'docs/retire.txt': b'old', 'docs/modified.txt': b'original'})
    installer = Installer(installer.root, archive, manifest)
    installer.execute(installer.plan('install'))
    (installer.root / 'docs/modified.txt').write_bytes(b'user changes')
    archive, manifest = make_payload(tmp_path, '0.5.0')
    newer = Installer(installer.root, archive, manifest)
    plan = newer.plan('update')
    assert 'preserved_modified_retired' in plan.warnings
    result = newer.execute(plan)
    assert not (newer.root / 'docs/retire.txt').exists()
    assert (newer.root / 'docs/modified.txt').read_bytes() == b'user changes'
    assert (result.backup_dir / 'program/docs/retire.txt').read_bytes() == b'old'
