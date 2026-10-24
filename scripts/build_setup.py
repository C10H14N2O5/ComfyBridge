"""Wrap a verified onedir release in one offline Setup, then smoke it before publishing.

Developer-only builder; end users need neither this script nor Python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def make_payload(release: Path, destination: Path) -> tuple[Path, Path]:
    """Use the actual finalized release, not a second approximation of its contents."""
    from comfybridge.installer_manifest import build_manifest, load_manifest
    from comfybridge import __version__

    if not (release / 'ComfyBridge.exe').is_file():
        raise ValueError('release_executable_missing')
    info = json.loads((release / 'BUILD-INFO.json').read_text(encoding='utf-8-sig'))
    if info.get('product') != 'ComfyBridge' or info.get('version') != __version__:
        raise ValueError('release_version_mismatch')
    destination.mkdir(parents=True, exist_ok=False)
    archive = destination / 'setup-payload.zip'
    manifest = destination / 'setup-manifest.json'
    document = build_manifest(release, archive)
    manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    load_manifest(manifest)  # The installed reader is also the builder's schema authority.
    return archive, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--legacy-release', type=Path)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Windows x64 builder required')
    release = args.release.resolve(strict=True)
    if release.parent != (ROOT / 'dist').resolve():
        parser.error('Release must be a direct child of this checkout dist directory')
    target = release.with_name(release.name + '-Setup.exe')
    if target.exists():
        parser.error('Final Setup already exists; choose a unique release name')
    work = ROOT / 'build' / ('setup-' + uuid.uuid4().hex)
    work.mkdir(parents=True)
    archive, manifest = make_payload(release, work / 'payload')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
                    '--name', 'Setup', '--paths', str(ROOT / 'src'),
                    '--distpath', str(work / 'dist'), '--workpath', str(work / 'work'),
                    '--specpath', str(work), '--add-data', str(archive) + os.pathsep + '.',
                    '--add-data', str(manifest) + os.pathsep + '.',
                    str(ROOT / 'scripts' / 'setup_entry.py')], check=True)
    setup = work / 'dist' / 'Setup.exe'
    smoke = [sys.executable, str(ROOT / 'scripts' / 'installer_smoke.py'), str(setup), str(release)]
    if args.legacy_release:
        smoke.extend(['--legacy-release', str(args.legacy_release.resolve(strict=True))])
    subprocess.run(smoke, check=True)
    # Publish only after both payload business smoke and real Setup smoke pass.
    shutil.copyfile(setup, target)
    result = {'file': target.name, 'bytes': target.stat().st_size, 'sha256': sha256(target),
              'payload_files': len(json.loads(manifest.read_text(encoding='utf-8'))['files']),
              'payload_archive_sha256': sha256(archive), 'signed': False}
    (ROOT / 'artifacts' / 'setup-build.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('RELEASE_SETUP=' + str(target))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
