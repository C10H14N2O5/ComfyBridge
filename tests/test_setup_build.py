"""Builder uses the same strict manifest reader as offline Setup."""
from pathlib import Path
import importlib.util
import json
import zipfile

import pytest

from comfybridge import __version__
from comfybridge.installer_manifest import load_manifest, validate_archive


SPEC = importlib.util.spec_from_file_location('comfybridge_setup_build', Path(__file__).resolve().parents[1] / 'scripts/build_setup.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def release_fixture(tmp_path):
    release = tmp_path / 'ComfyBridge-test'
    release.mkdir()
    (release / 'ComfyBridge.exe').write_bytes(b'test executable fixture')
    (release / 'BUILD-INFO.json').write_text(json.dumps({'product': 'ComfyBridge', 'version': __version__}), encoding='utf-8')
    (release / 'licenses').mkdir()
    (release / 'licenses/license.txt').write_text('test license fixture', encoding='utf-8')
    return release


def test_builder_inventory_exact_and_validated(tmp_path):
    release = release_fixture(tmp_path)
    archive, manifest_path = builder.make_payload(release, tmp_path / 'payload')
    manifest = load_manifest(manifest_path)
    validate_archive(archive, manifest)
    assert manifest.version == __version__
    assert manifest.build_id == release.name
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {'ComfyBridge.exe', 'BUILD-INFO.json', 'licenses/license.txt'}
    assert manifest.archive_sha256 == builder.sha256(archive)


def test_builder_rejects_mismatched_version(tmp_path):
    release = release_fixture(tmp_path)
    (release / 'BUILD-INFO.json').write_text(json.dumps({'product': 'ComfyBridge', 'version': '99.0.0'}), encoding='utf-8')
    with pytest.raises(ValueError, match='release_version_mismatch'):
        builder.make_payload(release, tmp_path / 'payload')


def test_builder_never_reuses_existing_output(tmp_path):
    release = release_fixture(tmp_path)
    destination = tmp_path / 'payload'
    destination.mkdir()
    with pytest.raises(FileExistsError):
        builder.make_payload(release, destination)


def test_builder_refuses_runtime_data_not_silently_bundles(tmp_path):
    release = release_fixture(tmp_path)
    (release / 'data').mkdir()
    (release / 'data/config.json').write_text('{"synthetic_key":"never_bundle"}', encoding='utf-8')
    with pytest.raises(ValueError):
        builder.make_payload(release, tmp_path / 'payload')
