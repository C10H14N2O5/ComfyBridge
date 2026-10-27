"""Regression tests for the release acceptance harness, without launching an EXE."""
from pathlib import Path
import importlib.util

import pytest


SPEC = importlib.util.spec_from_file_location(
    'comfybridge_installer_smoke', Path(__file__).resolve().parents[1] / 'scripts/installer_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


@pytest.mark.parametrize('previous', [set(), {'old-install-backup'}, {'first', 'second'}])
def test_upgrade_accepts_one_additional_backup_and_keeps_old_names(tmp_path, previous):
    for name in previous | {'new-upgrade-backup'}:
        (tmp_path / 'backup' / name).mkdir(parents=True)
    smoke.assert_one_new_backup(tmp_path, previous)


@pytest.mark.parametrize('current', [set(), {'new-backup'}, {'old-backup'}, {'old-backup', 'one', 'two'}])
def test_upgrade_rejects_missing_old_backup_or_wrong_new_backup_count(tmp_path, current):
    for name in current:
        (tmp_path / 'backup' / name).mkdir(parents=True)
    with pytest.raises(AssertionError):
        smoke.assert_one_new_backup(tmp_path, {'old-backup'})
