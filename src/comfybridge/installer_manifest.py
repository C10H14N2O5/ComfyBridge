"""Strict offline payload inventory. Hashes prove integrity, not publisher identity."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import zipfile

from .config import parse_json_strict

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_FILES = 50000
MAX_PAYLOAD_BYTES = 16 * 1024**3
RESERVED_ROOTS = frozenset({"data", "runtime", "backup", "backups", "comfybridge-backups", ".comfybridge-install"})
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class ManifestError(ValueError):
    def __init__(self, code="installer_payload_invalid"):
        self.code = code
        super().__init__(code)


def version_tuple(value: str) -> tuple[int, int, int]:
    if type(value) is not str or not VERSION.fullmatch(value) or len(value) > 32:
        raise ManifestError("installer_version_invalid")
    return tuple(int(part) for part in value.split("."))


def safe_relative(value: str) -> str:
    if (type(value) is not str or not value or len(value) > 240 or "\\" in value
            or ntpath.isabs(value) or ntpath.splitdrive(value)[0]
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ManifestError("installer_path_invalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.endswith((" ", ".")) or ntpath.isreserved(part) for part in parts):
        raise ManifestError("installer_path_invalid")
    if (parts[0].casefold() in RESERVED_ROOTS or parts[0].casefold().startswith(".comfybridge-")
            or parts[0].casefold().startswith("comfybridge-setup")
            or parts[0].casefold() in ("setup.exe", "setup-payload.zip", "setup-manifest.json")):
        raise ManifestError("installer_reserved_path")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str

    def as_dict(self):
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class Manifest:
    version: str
    build_id: str
    archive_sha256: str
    files: tuple[FileEntry, ...]
    schema_version: int = 1
    product: str = "ComfyBridge"
    platform: str = "windows-x64"
    launcher: str = "ComfyBridge.exe"

    @property
    def by_path(self):
        return {entry.path: entry for entry in self.files}

    def as_dict(self):
        return {"schema_version": self.schema_version, "product": self.product, "version": self.version,
                "build_id": self.build_id, "archive_sha256": self.archive_sha256,
                "platform": self.platform, "launcher": self.launcher,
                "files": [entry.as_dict() for entry in self.files]}


def load_manifest(source: Path | dict | Manifest) -> Manifest:
    if isinstance(source, Manifest):
        source = source.as_dict()
    try:
        if isinstance(source, (str, Path)):
            with Path(source).open("rb") as stream:
                content = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ManifestError()
            source = parse_json_strict(content)
        fields = {"schema_version", "product", "version", "build_id", "archive_sha256", "platform", "launcher", "files"}
        if (type(source) is not dict or set(source) != fields or type(source["schema_version"]) is not int
                or source["schema_version"] != 1 or source["product"] != "ComfyBridge"
                or source["platform"] != "windows-x64" or source["launcher"] != "ComfyBridge.exe"
                or type(source["build_id"]) is not str
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", source["build_id"])
                or type(source["archive_sha256"]) is not str or not DIGEST.fullmatch(source["archive_sha256"])
                or type(source["files"]) is not list or not 1 <= len(source["files"]) <= MAX_FILES):
            raise ManifestError()
        version_tuple(source["version"])
        files, names, parents, total = [], set(), set(), 0
        for entry in source["files"]:
            if (type(entry) is not dict or set(entry) != {"path", "size", "sha256"}
                    or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_PAYLOAD_BYTES
                    or type(entry["sha256"]) is not str or not DIGEST.fullmatch(entry["sha256"])):
                raise ManifestError()
            name = safe_relative(entry["path"])
            folded = name.casefold()
            components = folded.split("/")
            prefixes = {"/".join(components[:index]) for index in range(1, len(components))}
            if folded in names or folded in parents or prefixes & names:
                raise ManifestError("installer_payload_collision")
            names.add(folded)
            parents.update(prefixes)
            total += entry["size"]
            files.append(FileEntry(name, entry["size"], entry["sha256"]))
        if total > MAX_PAYLOAD_BYTES or source["launcher"] not in {entry.path for entry in files}:
            raise ManifestError()
        return Manifest(source["version"], source["build_id"], source["archive_sha256"], tuple(files))
    except ManifestError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise ManifestError() from None


def validate_archive(archive: Path, manifest: Manifest) -> None:
    """Hash the whole ZIP before parsing; require exactly the declared regular files."""
    manifest = load_manifest(manifest)
    try:
        if sha256_file(archive) != manifest.archive_sha256:
            raise ManifestError("installer_archive_digest")
        expected = manifest.by_path
        seen = set()
        with zipfile.ZipFile(archive) as package:
            if len(package.infolist()) > MAX_FILES * 2:
                raise ManifestError()
            for item in package.infolist():
                path = safe_relative(item.filename[:-1] if item.is_dir() else item.filename)
                folded = path.casefold()
                if folded in seen or item.flag_bits & 1:
                    raise ManifestError("installer_payload_collision")
                seen.add(folded)
                mode = (item.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ManifestError("installer_payload_link")
                if item.is_dir():
                    if not any(name.startswith(path + "/") for name in expected):
                        raise ManifestError()
                    continue
                if stat.S_ISDIR(mode) or path not in expected or item.file_size != expected[path].size:
                    raise ManifestError()
                digest, count = hashlib.sha256(), 0
                with package.open(item) as stream:
                    while chunk := stream.read(1024 * 1024):
                        count += len(chunk)
                        if count > expected[path].size:
                            raise ManifestError()
                        digest.update(chunk)
                if count != expected[path].size or digest.hexdigest() != expected[path].sha256:
                    raise ManifestError("installer_payload_digest")
        if not {name.casefold() for name in expected} <= seen:
            raise ManifestError()
    except ManifestError:
        raise
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, EOFError):
        raise ManifestError() from None


def extract_archive(archive: Path, manifest: Manifest, destination: Path) -> None:
    """Caller owns a fresh confined stage; never uses ZipFile.extract/all."""
    from .install_platform import assert_safe_path
    validate_archive(archive, manifest)
    with zipfile.ZipFile(archive) as package:
        for entry in manifest.files:
            target = assert_safe_path(destination / entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            assert_safe_path(target.parent)
            with package.open(entry.path) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if target.stat().st_size != entry.size or sha256_file(target) != entry.sha256:
                raise ManifestError("installer_payload_digest")


def build_manifest(release_dir: Path, archive_path: Path) -> dict:
    """Build helper: reject forbidden files rather than silently excluding them."""
    import os
    from .install_platform import assert_safe_path
    root = assert_safe_path(Path(release_dir), allow_missing=False)
    info = parse_json_strict((root / "BUILD-INFO.json").read_bytes())
    if not isinstance(info, dict) or info.get("product") != "ComfyBridge":
        raise ManifestError("installer_product_invalid")
    version_tuple(info.get("version"))
    files = []
    for directory, folders, names in os.walk(root, followlinks=False):
        for name in folders + names:
            assert_safe_path(Path(directory) / name, allow_missing=False)
        for name in names:
            path = Path(directory) / name
            relative = safe_relative(path.relative_to(root).as_posix())
            files.append(FileEntry(relative, path.stat().st_size, sha256_file(path)))
    files.sort(key=lambda entry: entry.path)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for entry in files:
            package.write(root / entry.path, entry.path)
    manifest = Manifest(info["version"], root.name, sha256_file(archive_path), tuple(files))
    return load_manifest(manifest).as_dict()
