import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

from comfybridge.installer_manifest import (ManifestError, build_manifest, extract_archive,
    load_manifest, safe_relative, sha256_file, validate_archive, version_tuple)


def payload(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    (release / 'ComfyBridge.exe').write_bytes(b'fake executable')
    (release / 'BUILD-INFO.json').write_text(json.dumps({'product': 'ComfyBridge', 'version': '0.4.0'}))
    (release / '_internal').mkdir()
    (release / '_internal' / 'library.dll').write_bytes(b'library')
    archive = tmp_path / 'payload.zip'
    return release, archive, build_manifest(release, archive)


@pytest.mark.parametrize('path', ['', '../x', '/x', 'C:/x', 'C:x', '//host/x', 'a\\b', 'a/../b', 'a//b',
    'a/./b', 'a.', 'a ', 'NUL', 'con.txt', 'a/AUX.exe', 'x:y', 'x\x00y', 'x\x1fy', '\ud800',
    'data/x', 'DATA/x', 'backup/x', 'backups/x', 'ComfyBridge-backups/x', '.comfybridge-install/x',
    'runtime/x', '.comfybridge-private/x', 'ComfyBridge-Setup.exe', 'setup.exe', 'setup-payload.zip'])
def test_rejects_unsafe_and_reserved_paths(path):
    with pytest.raises(ManifestError):
        safe_relative(path)


@pytest.mark.parametrize('version', ['v0.4.0', '0.4', '00.4.0', '0.4.0-dev', '', None, True, '0.-1.0'])
def test_versions_strict(version):
    with pytest.raises(ManifestError):
        version_tuple(version)


def test_roundtrip_build_validate_extract(tmp_path):
    release, archive, document = payload(tmp_path)
    manifest = load_manifest(document)
    assert manifest.as_dict() == document
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(document))
    assert load_manifest(path) == manifest
    validate_archive(archive, manifest)
    destination = tmp_path / 'stage'
    destination.mkdir()
    extract_archive(archive, manifest, destination)
    for entry in manifest.files:
        assert (destination / entry.path).read_bytes() == (release / entry.path).read_bytes()


@pytest.mark.parametrize('field,value', [('schema_version', True), ('schema_version', 2), ('product', 'Other'),
    ('platform', 'linux'), ('launcher', 'other.exe'), ('build_id', '../build'), ('archive_sha256', 'A' * 64),
    ('files', []), ('version', '0.4.0+test')])
def test_manifest_fields_strict(tmp_path, field, value):
    _, _, document = payload(tmp_path)
    document[field] = value
    with pytest.raises(ManifestError):
        load_manifest(document)


@pytest.mark.parametrize('change', ['extra', 'size_bool', 'negative', 'duplicate', 'casefold', 'prefix', 'missing_launcher'])
def test_manifest_inventory_strict(tmp_path, change):
    _, _, document = payload(tmp_path)
    first = document['files'][0]
    if change == 'extra': document['extra'] = 1
    elif change == 'size_bool': first['size'] = True
    elif change == 'negative': first['size'] = -1
    elif change == 'duplicate': document['files'].append(dict(first))
    elif change == 'casefold': document['files'].append(dict(first, path=first['path'].swapcase()))
    elif change == 'prefix': document['files'].append(dict(first, path=first['path'] + '/child'))
    elif change == 'missing_launcher': document['files'] = [item for item in document['files'] if item['path'] != 'ComfyBridge.exe']
    with pytest.raises(ManifestError): load_manifest(document)


def test_duplicate_json_key_rejected(tmp_path):
    path = tmp_path / 'manifest.json'
    path.write_text('{"product":"ComfyBridge","product":"Other"}')
    with pytest.raises(ManifestError): load_manifest(path)


def test_whole_archive_hash_checked_before_zip_parse(tmp_path, monkeypatch):
    _, archive, document = payload(tmp_path)
    archive.write_bytes(b'not a zip')
    monkeypatch.setattr(zipfile, 'ZipFile', lambda *_args, **_kwargs: pytest.fail('ZIP opened before digest'))
    with pytest.raises(ManifestError, match='installer_archive_digest'):
        validate_archive(archive, load_manifest(document))


@pytest.mark.parametrize('kind', ['extra', 'missing', 'corrupt', 'symlink', 'duplicate', 'traversal', 'directory_extra'])
def test_archive_exact_regular_inventory(tmp_path, kind):
    release, archive, document = payload(tmp_path)
    entries = {item['path']: (release / item['path']).read_bytes() for item in document['files']}
    with zipfile.ZipFile(archive, 'w') as package:
        for name, content in entries.items():
            if kind == 'missing' and name == 'ComfyBridge.exe': continue
            if kind == 'corrupt' and name == 'ComfyBridge.exe': content = b'wrong'
            if kind == 'symlink' and name == 'ComfyBridge.exe':
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(info, content)
            else: package.writestr(name, content)
        if kind == 'extra': package.writestr('extra.txt', b'unknown')
        if kind == 'duplicate':
            with pytest.warns(UserWarning): package.writestr('ComfyBridge.exe', entries['ComfyBridge.exe'])
        if kind == 'traversal': package.writestr('../outside', b'bad')
        if kind == 'directory_extra': package.writestr('empty/', b'')
    document['archive_sha256'] = sha256_file(archive)
    with pytest.raises(ManifestError): validate_archive(archive, load_manifest(document))


def test_builder_refuses_data_not_silent_exclusion(tmp_path):
    release, archive, _ = payload(tmp_path)
    (release / 'data').mkdir()
    (release / 'data' / 'config.json').write_text('private')
    with pytest.raises(ManifestError): build_manifest(release, archive)


def test_extract_fsyncs_each_file(tmp_path, monkeypatch):
    _, archive, document = payload(tmp_path)
    calls = []
    monkeypatch.setattr('comfybridge.installer_manifest.os.fsync', lambda fd: calls.append(fd))
    destination = tmp_path / 'stage'
    destination.mkdir()
    extract_archive(archive, load_manifest(document), destination)
    assert len(calls) == len(document['files'])
