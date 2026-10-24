"""Offline install/update transactions; user data is copied, never migrated/restored.

The installed receipt records ownership, not a signature. Legacy checksums are
only constrained inventory hints: legacy old-only files are never deleted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Callable
import uuid

from .config import parse_json_strict
from .installer_manifest import (FileEntry, Manifest, ManifestError, load_manifest,
                                 safe_relative, sha256_file, validate_archive, extract_archive,
                                 version_tuple, MAX_MANIFEST_BYTES, DIGEST)
from . import install_platform as platform

STATE = ".comfybridge-install"
BACKUPS = "backup"
LAUNCHER = "ComfyBridge.exe"
LEGACY_ROOT_FILES = frozenset({LAUNCHER, "BUILD-INFO.json", "SHA256SUMS.txt", "README.md", "CHANGELOG.md",
                              "ARCHITECTURE.md", "todo.md", "pyproject.toml", "uv.lock", ".gitignore", ".python-version"})
LEGACY_ROOT_DIRS = frozenset({"_internal", "bin", "docs", "examples", "licenses", "src", "tests", "scripts", "third-party-sources"})


class InstallerError(RuntimeError):
    def __init__(self, code: str, *, backup_path: Path | None = None):
        self.code = code
        self.backup_path = backup_path
        super().__init__(code)


def _error(error):
    if isinstance(error, InstallerError):
        return error
    code = getattr(error, "code", "")
    if isinstance(error, ManifestError):
        return InstallerError(code)
    if isinstance(error, platform.InstallPlatformError):
        return InstallerError("installer_" + code.removeprefix("install_"))
    return InstallerError("installer_io_error" if isinstance(error, OSError) else "installer_state_invalid")


def _public(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except InstallerError:
            raise
        except (ManifestError, platform.InstallPlatformError, OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
            raise _error(error) from None
    return wrapped


@dataclass(frozen=True)
class Inspection:
    root: Path
    target_version: str
    current_version: str | None
    modes: tuple[str, ...]
    modified_managed: tuple[str, ...] = ()
    unknown_files: tuple[str, ...] = ()
    pending: bool = False
    installed: bool = False

    installed_version = property(lambda self: self.current_version)
    managed_modified = property(lambda self: self.modified_managed)
    recovery_required = property(lambda self: self.pending)


@dataclass(frozen=True)
class InstallPlan:
    root: Path
    mode: str
    current_version: str | None
    target_version: str
    data_dir: Path
    backup_path: Path
    modified_managed: tuple[str, ...]
    warnings: tuple[str, ...]
    transaction_id: str
    repair: bool
    _fingerprint: str = field(repr=False)
    _owner: str = field(repr=False)

    old_version = property(lambda self: self.current_version)
    new_version = property(lambda self: self.target_version)
    backup_dir = property(lambda self: self.backup_path)
    managed_modified = property(lambda self: self.modified_managed)


@dataclass(frozen=True)
class InstallResult:
    root: Path
    version: str
    mode: str
    data_dir: Path
    backup_path: Path
    transaction_id: str
    selftest_passed: bool = True
    _owner: str = field(default="", repr=False)

    backup_dir = property(lambda self: self.backup_path)


@dataclass(frozen=True)
class RecoveryResult:
    recovered: bool
    outcome: str
    backup_path: Path | None = None

    backup_dir = property(lambda self: self.backup_path)


def _read_json(path: Path, cap=MAX_MANIFEST_BYTES):
    platform.assert_safe_path(path, allow_missing=False)
    with path.open("rb") as stream:
        content = stream.read(cap + 1)
    if len(content) > cap:
        raise InstallerError("installer_state_invalid")
    return parse_json_strict(content)


def _atomic_json(path: Path, value: object):
    platform.assert_safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    platform.protect_private(path.parent)
    temporary = path.with_name(".write-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            platform.protect_private(temporary)
            stream.write((json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        platform.assert_safe_path(path)
        os.replace(temporary, path)
        with path.open("r+b") as committed:
            os.fsync(committed.fileno())
        if not platform.verify_private(path):
            raise InstallerError("installer_acl_failed")
    finally:
        temporary.unlink(missing_ok=True)


def _matches(path: Path, entry: dict | FileEntry) -> bool:
    platform.assert_safe_path(path)
    if not path.is_file():
        return False
    size = entry.size if isinstance(entry, FileEntry) else entry["size"]
    digest = entry.sha256 if isinstance(entry, FileEntry) else entry["sha256"]
    return path.stat().st_size == size and sha256_file(path) == digest


def _remove_tree(path: Path):
    """Delete only a validated dedicated transaction/backup tree, without following links."""
    platform.assert_safe_path(path)
    if not path.exists():
        return
    for directory, folders, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            target = platform.assert_safe_path(Path(directory) / name, allow_missing=False)
            target.unlink()
        for name in folders:
            target = platform.assert_safe_path(Path(directory) / name, allow_missing=False)
            target.rmdir()
    path.rmdir()


class Installer:
    @_public
    def __init__(self, root: Path, payload_zip: Path, payload_manifest: Path | dict | Manifest,
                 setup_path: Path | None = None):
        self.root = platform.validate_local_ntfs(Path(root), allow_missing=False)
        if not self.root.is_dir():
            raise InstallerError("installer_path_invalid")
        self.payload_zip = platform.assert_safe_path(Path(payload_zip), allow_missing=False)
        if isinstance(payload_manifest, (Path, str)):
            platform.assert_safe_path(Path(payload_manifest), allow_missing=False)
        self.manifest = load_manifest(payload_manifest)
        self.setup_path = platform.assert_safe_path(Path(setup_path), allow_missing=False) if setup_path is not None else None
        self.state = self.root / STATE
        self.marker = self.state / "transaction.json"
        self.receipt_path = self.state / "receipt.json"
        self._owner = uuid.uuid4().hex
        self._results = {}
        self._plans = {}
        self._payload_validated = False
        self._payload_manifest_path = Path(payload_manifest).absolute() if isinstance(payload_manifest, (Path, str)) else None

    def _checkpoint(self, point: str, path: str = ""):
        """Fault injection seam; production has no hooks or arbitrary code execution."""

    def _receipt(self):
        platform.assert_safe_path(self.receipt_path)
        if not self.receipt_path.exists():
            return None
        document = _read_json(self.receipt_path)
        if (type(document) is not dict or set(document) != {"schema_version", "transaction_id", "manifest"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or str(uuid.UUID(document["transaction_id"])) != document["transaction_id"]):
            raise InstallerError("installer_receipt_invalid")
        load_manifest(document["manifest"])
        return document

    def _excluded_root(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        first = relative.parts[0].casefold()
        return (first in {STATE.casefold(), BACKUPS.casefold(), "comfybridge-backups", "backup", "backups", "data", "runtime"}
                or path == self.setup_path or path == self.payload_zip or path == self._payload_manifest_path)

    def _walk(self, root: Path, *, exclude: Callable[[Path], bool] = lambda _: False):
        platform.assert_safe_path(root)
        files, directories = {}, []
        if not root.exists():
            return files, directories
        if not root.is_dir():
            raise InstallerError("installer_path_invalid")
        for directory, folders, names in os.walk(root, followlinks=False):
            parent = Path(directory)
            folders[:] = [name for name in sorted(folders) if not exclude(parent / name)]
            for name in folders:
                child = platform.assert_safe_path(parent / name, allow_missing=False)
                directories.append(child.relative_to(root).as_posix())
            for name in sorted(names):
                child = parent / name
                if exclude(child) or (root != self.root and child == root / ".comfybridge-data.lease"):
                    continue
                platform.assert_safe_path(child, allow_missing=False)
                files[child.relative_to(root).as_posix()] = {"size": child.stat().st_size, "sha256": sha256_file(child)}
        folded = [name.casefold() for name in files]
        if len(set(folded)) != len(folded):
            raise InstallerError("installer_path_collision")
        return files, directories

    def _inventory(self):
        actual, directories = self._walk(self.root, exclude=self._excluded_root)
        receipt = self._receipt()
        if receipt:
            old = load_manifest(receipt["manifest"])
            build = self.root / "BUILD-INFO.json"
            info = None
            if build.exists():
                try:
                    info = _read_json(build)
                except (ValueError, UnicodeError):
                    pass  # A receipt-owned damaged metadata file is repairable.
            if isinstance(info, dict):
                if isinstance(info.get("product"), str) and info["product"] != "ComfyBridge":
                    raise InstallerError("installer_product_invalid")
                try:
                    parsed_version = version_tuple(info.get("version"))
                except ManifestError:
                    parsed_version = None
                if parsed_version is not None and info["version"] != old.version:
                    raise InstallerError("installer_product_invalid")
            return old.version, {entry.path: entry.as_dict() for entry in old.files}, actual, receipt, False
        build_path = self.root / "BUILD-INFO.json"
        launcher = self.root / LAUNCHER
        if build_path.exists() or launcher.exists():
            if not build_path.is_file() or not launcher.is_file() or not (self.root / "_internal").is_dir():
                raise InstallerError("installer_product_invalid")
            info = _read_json(build_path)
            if not isinstance(info, dict) or info.get("product") != "ComfyBridge" or info.get("version") not in ("0.1.0", "0.2.0", "0.3.0"):
                raise InstallerError("installer_product_invalid")
            checksums = self.root / "SHA256SUMS.txt"
            platform.assert_safe_path(checksums, allow_missing=False)
            with checksums.open("rb") as stream:
                content = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise InstallerError("installer_product_invalid")
            managed = {}
            for line in content.decode("utf-8").splitlines():
                if not line.strip():
                    continue
                digest, separator, relative = line.partition("  ")
                if not separator or not DIGEST.fullmatch(digest):
                    raise InstallerError("installer_product_invalid")
                safe_relative(relative)
                # Old SHA256SUMS is NOT arbitrary ownership/deletion authority.
                # Only paths shared with the trusted new payload in known product
                # layout may be treated as legacy replacement candidates.
                if relative not in self.manifest.by_path:
                    continue
                parts = relative.split("/")
                if not (relative in LEGACY_ROOT_FILES or (len(parts) > 1 and parts[0] in LEGACY_ROOT_DIRS)):
                    continue
                if relative in managed:
                    raise InstallerError("installer_product_invalid")
                managed[relative] = {"path": relative, "size": actual.get(relative, {}).get("size", -1), "sha256": digest}
            if LAUNCHER not in managed or "BUILD-INFO.json" not in managed:
                raise InstallerError("installer_product_invalid")
            # The inventory itself is a fixed product path, although not self-hashed.
            if checksums.name in self.manifest.by_path:
                managed[checksums.name] = {"path": checksums.name, **actual[checksums.name]}
            return info["version"], managed, actual, None, True
        if actual or directories:
            raise InstallerError("installer_install_not_empty")
        # A fresh directory may contain Setup and our empty coordination remnants,
        # not unrelated empty folders or pre-existing business data.
        for child in self.root.iterdir():
            platform.assert_safe_path(child, allow_missing=False)
            folded = child.name.casefold()
            if child.is_file() and (child == self.setup_path or child == self.payload_zip
                    or child == self._payload_manifest_path):
                continue
            if child == self.root / "data" and child.is_dir():
                if any(item.name != ".comfybridge-data.lease" or not item.is_file() or item.stat().st_size != 0
                       for item in child.iterdir()):
                    raise InstallerError("installer_install_not_empty")
                continue
            if child == self.state and child.is_dir():
                for item in child.iterdir():
                    if item.name == "install.lease" and item.is_file() and item.stat().st_size == 0:
                        continue
                    try:
                        empty_transaction = item.is_dir() and str(uuid.UUID(item.name)) == item.name and not any(item.iterdir())
                    except ValueError:
                        empty_transaction = False
                    if not empty_transaction:
                        raise InstallerError("installer_install_not_empty")
                continue
            if child == self.root / BACKUPS and child.is_dir():
                for item in child.iterdir():
                    try:
                        metadata = _read_json(item / "backup.json")
                        valid = (item.is_dir() and str(uuid.UUID(item.name)) == item.name
                                 and metadata.get("transaction_id") == item.name and metadata.get("root") == str(self.root))
                    except (OSError, ValueError, TypeError, InstallerError):
                        valid = False
                    if not valid:
                        raise InstallerError("installer_install_not_empty")
                continue
            raise InstallerError("installer_install_not_empty")
        return None, {}, actual, None, False

    @_public
    def inspect(self) -> Inspection:
        platform.assert_safe_path(self.marker)
        if self.marker.exists():
            return Inspection(self.root, self.manifest.version, None, (), pending=True)
        current, managed, actual, _, _ = self._inventory()
        if current is None:
            modes = ("install",)
        elif version_tuple(current) > version_tuple(self.manifest.version):
            modes = ()
        elif current == self.manifest.version:
            modes = ("repair",)
        else:
            modes = ("update",)
        folded = {name.casefold(): name for name in actual}
        modified = tuple(sorted(name for name, entry in managed.items()
                                if name.casefold() not in folded or not _matches(self.root / name, entry)))
        owned = {name.casefold() for name in managed}
        unknown = tuple(sorted(name for name in actual if name.casefold() not in owned))
        return Inspection(self.root, self.manifest.version, current, modes, modified, unknown, installed=current is not None)

    def _data_path(self, data_dir):
        data = platform.validate_local_ntfs(Path(data_dir) if data_dir is not None else self.root / "data")
        platform.assert_no_overlap(self.root, data)
        if not data.is_relative_to(self.root) and not data.is_dir():
            raise InstallerError("installer_data_missing")
        return data

    def _fingerprint(self, current, managed, actual, receipt):
        value = {"current": current, "managed": managed, "actual": actual, "receipt": receipt,
                 "payload": self.manifest.as_dict()}
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @_public
    def plan(self, mode: str, data_dir: Path | None = None) -> InstallPlan:
        if self.marker.exists():
            raise InstallerError("installer_recovery_required")
        data = self._data_path(data_dir)
        with platform.SetupGuard(self.root, data, [self.root / entry.path for entry in self.manifest.files],
                                 setup_path=self.setup_path, probe_only=True):
            summary = self.inspect()
            if summary.current_version and version_tuple(summary.current_version) > version_tuple(self.manifest.version):
                raise InstallerError("installer_downgrade")
            if mode not in summary.modes:
                raise InstallerError("installer_mode_invalid")
            current, managed, actual, receipt, legacy = self._inventory()
            owned = {name.casefold() for name in managed}
            for name in self.manifest.by_path:
                found = next((item for item in actual if item.casefold() == name.casefold()), None)
                if found is not None and name.casefold() not in owned:
                    raise InstallerError("installer_unknown_collision")
                target = self.root / name
                platform.assert_safe_path(target)
                if target.exists() and not target.is_file():
                    raise InstallerError("installer_unknown_collision")
            self._walk(data)  # Fail closed on links/hardlinks before any backup claim.
            warnings = []
            if legacy:
                warnings.extend(("legacy_inventory_untrusted", "legacy_retired_preserved"))
            if summary.modified_managed:
                warnings.append("modified_managed")
            if not data.is_relative_to(self.root):
                warnings.append("external_data_backup_only")
            if any(name not in self.manifest.by_path and not _matches(self.root / name, entry)
                   for name, entry in managed.items() if (self.root / name).exists()):
                warnings.append("preserved_modified_retired")
            identifier = str(uuid.uuid4())
            self._check_space(data)
            plan = InstallPlan(self.root, mode, current, self.manifest.version, data,
                               self.root / BACKUPS / identifier, summary.modified_managed, tuple(warnings),
                               identifier, mode == "repair", self._fingerprint(current, managed, actual, receipt), self._owner)
            self._plans[identifier] = plan
            return plan

    prepare = plan

    def _progress(self, callback, stage, completed=0, total=0):
        if callback is not None:
            callback(stage, completed, total)

    def _save(self, transaction):
        _atomic_json(self.marker, transaction)

    def _backup_exclude(self, data):
        def excluded(path):
            relative = path.relative_to(self.root)
            first = relative.parts[0].casefold()
            return (first in {STATE.casefold(), BACKUPS.casefold(), "comfybridge-backups", "backup", "backups"}
                    or path == self.setup_path or path == self.payload_zip or path == self._payload_manifest_path
                    or path == data)
        return excluded

    def _check_space(self, data):
        program, _ = self._walk(self.root, exclude=self._backup_exclude(data))
        selected, _ = self._walk(data)
        # ZIP remains at its source; include its size as a conservative reserve.
        needed = (sum(entry.size for entry in self.manifest.files) + self.payload_zip.stat().st_size
                  + sum(entry["size"] for entry in program.values())
                  + sum(entry["size"] for entry in selected.values())
                  + (len(program) + len(selected) + len(self.manifest.files)) * 8192 + 32 * 1024**2)
        if shutil.disk_usage(self.root).free < needed:
            raise InstallerError("installer_disk_space")

    def _backup(self, transaction, progress):
        destination = self.root / BACKUPS / transaction["transaction_id"]
        platform.assert_safe_path(destination)
        if destination.exists():
            raise InstallerError("installer_backup_collision")
        destination.mkdir(parents=True)
        # Never alter ACLs of a pre-existing user backup parent.
        platform.protect_private(destination)
        data = Path(transaction["data_dir"])
        _atomic_json(destination / "backup.json", {"schema_version": 1, "transaction_id": transaction["transaction_id"],
                     "root": str(self.root), "data_dir": transaction["data_dir"], "verified": False,
                     "previous_receipt": transaction["previous_receipt"], "inventory": {}})
        sources = [(self.root, destination / "program", self._backup_exclude(data)),
                   (data, destination / "data", lambda _: False)]
        inventory = {}
        for source, target, exclude in sources:
            files, directories = self._walk(source, exclude=exclude)
            target.mkdir()
            platform.protect_private(target)
            for relative in directories:
                folder = platform.assert_safe_path(target / relative)
                folder.mkdir(parents=True, exist_ok=True)
                platform.protect_private(folder)
            for index, (relative, entry) in enumerate(files.items(), 1):
                original = platform.assert_safe_path(source / relative, allow_missing=False)
                copied = platform.assert_safe_path(target / relative)
                copied.parent.mkdir(parents=True, exist_ok=True)
                with original.open("rb") as incoming, copied.open("xb") as outgoing:
                    platform.protect_private(copied)
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if not _matches(original, entry) or not _matches(copied, entry) or not platform.verify_private(copied):
                    raise InstallerError("installer_backup_verify_failed")
                self._checkpoint("backup_file", relative)
                self._progress(progress, "backup", index, len(files))
            # Detect new/deleted/changed files during the copy, not just copied bytes.
            if self._walk(source, exclude=exclude)[0] != files:
                raise InstallerError("installer_source_changed")
            inventory[target.name] = {"files": files, "directories": directories}
        platform.protect_private_tree(destination)
        _atomic_json(destination / "backup.json", {"schema_version": 1, "transaction_id": transaction["transaction_id"],
                     "root": str(self.root), "data_dir": transaction["data_dir"], "verified": True,
                     "previous_receipt": transaction["previous_receipt"], "inventory": inventory})
        transaction["backup_verified"] = True
        self._save(transaction)
        self._checkpoint("backup_verified")

    def _self_test(self, executable):
        result = platform.validate_self_test(executable)
        if (type(result) is not dict or result.get("product") != "ComfyBridge" or result.get("version") != self.manifest.version
                or type(result.get("schema")) is not int or result["schema"] != 1 or result.get("ok") is not True
                or result.get("checks") != {"imports": True, "ffmpeg": True, "ffprobe": True}):
            raise InstallerError("installer_selftest_failed")

    def _apply_action(self, transaction, kind, relative, guard):
        work = self.state / transaction["transaction_id"]
        action = {"kind": kind, "path": relative}
        transaction["actions"].append(action)
        self._save(transaction)  # WAL before each individual destructive rename.
        self._checkpoint("before_" + kind, relative)
        if kind == "withdraw":
            source, target = self.root / relative, work / "old" / relative
            expected = transaction["original_files"][relative]
        else:
            source, target = work / "stage" / relative, self.root / relative
            expected = self.manifest.by_path[relative]
        if not _matches(source, expected):
            raise InstallerError("installer_source_changed")
        platform.assert_safe_path(target)
        platform.check_replace_access([source, target])
        if target.exists():
            raise InstallerError("installer_unknown_collision")
        if kind == "publish":
            current = self.root
            for component in target.parent.relative_to(self.root).parts:
                current = platform.assert_safe_path(current / component)
                if not current.exists():
                    directory = current.relative_to(self.root).as_posix()
                    transaction["created_directories"].append(directory)
                    self._save(transaction)  # Directory intent precedes creation.
                    current.mkdir()
                    self._checkpoint("created_directory", directory)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        self._checkpoint("after_" + kind, relative)

    @_public
    def execute(self, plan: InstallPlan, progress=None) -> InstallResult:
        if (not isinstance(plan, InstallPlan) or plan._owner != self._owner or plan.root != self.root
                or self._plans.get(plan.transaction_id) is not plan):
            raise InstallerError("installer_plan_invalid")
        if plan.backup_path != self.root / BACKUPS / plan.transaction_id:
            raise InstallerError("installer_plan_invalid")
        # Only acquisition may report retryable busy. Once work starts all errors
        # roll back and are nonretryable with this old plan/transaction identity.
        with platform.SetupGuard(self.root, plan.data_dir, [self.root / entry.path for entry in self.manifest.files],
                                 setup_path=self.setup_path) as guard:
            if self.marker.exists():
                raise InstallerError("installer_recovery_required")
            current, managed, actual, receipt, legacy = self._inventory()
            if self._fingerprint(current, managed, actual, receipt) != plan._fingerprint:
                raise InstallerError("installer_plan_stale")
            self._progress(progress, "validate")
            self._check_space(plan.data_dir)
            validate_archive(self.payload_zip, self.manifest)
            original = {}
            for name, entry in managed.items():
                if name in self.manifest.by_path or (not legacy and _matches(self.root / name, entry)):
                    if (self.root / name).is_file():
                        original[name] = {"size": (self.root / name).stat().st_size, "sha256": sha256_file(self.root / name)}
            transaction = {"schema_version": 1, "transaction_id": plan.transaction_id, "root": str(self.root),
                           "data_dir": str(plan.data_dir), "mode": plan.mode, "phase": "preparing",
                           "manifest": self.manifest.as_dict(), "previous_receipt": receipt,
                           "original_files": original, "actions": [], "created_directories": [], "backup_verified": False}
            work = self.state / plan.transaction_id
            if work.exists():
                raise InstallerError("installer_plan_stale")
            work.mkdir(parents=True)
            platform.protect_private(self.state)
            platform.protect_private(work)
            self._save(transaction)
            self._plans.pop(plan.transaction_id)
            try:
                stage = work / "stage"
                stage.mkdir()
                platform.protect_private(stage)
                self._progress(progress, "stage")
                extract_archive(self.payload_zip, self.manifest, stage)
                platform.protect_private_tree(stage)
                self._checkpoint("staged")
                self._progress(progress, "self_test")
                self._self_test(stage / LAUNCHER)
                self._checkpoint("staged_selftest")
                self._backup(transaction, progress)
                guard.recheck()
                transaction["phase"] = "applying"
                self._save(transaction)
                if LAUNCHER in original:
                    self._apply_action(transaction, "withdraw", LAUNCHER, guard)
                    guard.recheck()  # Fence a legacy launcher racing the first withdrawal.
                for index, name in enumerate(sorted((set(original) | set(self.manifest.by_path)) - {LAUNCHER}), 1):
                    if name in original:
                        self._apply_action(transaction, "withdraw", name, guard)
                    if name in self.manifest.by_path:
                        self._apply_action(transaction, "publish", name, guard)
                    self._progress(progress, "apply", index, len(self.manifest.files))
                self._apply_action(transaction, "publish", LAUNCHER, guard)
                self._progress(progress, "verify")
                for entry in self.manifest.files:
                    if not _matches(self.root / entry.path, entry):
                        raise InstallerError("installer_installed_verify_failed")
                self._self_test(self.root / LAUNCHER)
                self._checkpoint("installed_selftest")
                transaction["phase"] = "committing"
                self._save(transaction)
                new_receipt = {"schema_version": 1, "transaction_id": plan.transaction_id, "manifest": self.manifest.as_dict()}
                self._checkpoint("before_receipt")
                _atomic_json(self.receipt_path, new_receipt)
                self._checkpoint("after_receipt")
                transaction["phase"] = "committed"
                self._save(transaction)
                self._checkpoint("committed")
                self.marker.unlink()
            except Exception as error:
                try:
                    self._progress(progress, "rollback")
                    self._rollback(transaction)
                except Exception:
                    raise InstallerError("installer_rollback_failed", backup_path=plan.backup_path) from None
                translated = _error(error)
                if translated.code == "installer_busy":
                    translated = InstallerError("installer_operation_failed")
                raise InstallerError(translated.code, backup_path=plan.backup_path if plan.backup_path.exists() else None) from None
            # Commit is durable. Cleanup failures must not roll back a live receipt.
            try:
                _remove_tree(work)
            except (OSError, ValueError, RuntimeError):
                pass
            result = InstallResult(self.root, self.manifest.version, plan.mode, plan.data_dir, plan.backup_path,
                                   plan.transaction_id, True, self._owner)
            self._results[plan.transaction_id] = result
            self._progress(progress, "commit", len(self.manifest.files), len(self.manifest.files))
            return result

    def _validate_transaction(self, transaction):
        fields = {"schema_version", "transaction_id", "root", "data_dir", "mode", "phase", "manifest",
                  "previous_receipt", "original_files", "actions", "created_directories", "backup_verified"}
        if (type(transaction) is not dict or set(transaction) != fields
                or type(transaction["schema_version"]) is not int or transaction["schema_version"] != 1
                or transaction["root"] != str(self.root)
                or str(uuid.UUID(transaction["transaction_id"])) != transaction["transaction_id"]
                or transaction["phase"] not in ("preparing", "applying", "committing", "committed", "rolling_back")
                or transaction["mode"] not in ("install", "update", "repair")
                or type(transaction["backup_verified"]) is not bool
                or type(transaction["original_files"]) is not dict or type(transaction["actions"]) is not list
                or type(transaction["created_directories"]) is not list):
            raise InstallerError("installer_state_invalid")
        new = load_manifest(transaction["manifest"])
        allowed = set(new.by_path)
        previous = transaction["previous_receipt"]
        if previous is not None:
            if (type(previous) is not dict or set(previous) != {"schema_version", "transaction_id", "manifest"}
                    or type(previous["schema_version"]) is not int or previous["schema_version"] != 1
                    or str(uuid.UUID(previous["transaction_id"])) != previous["transaction_id"]):
                raise InstallerError("installer_state_invalid")
            allowed.update(load_manifest(previous["manifest"]).by_path)
        for name, entry in transaction["original_files"].items():
            safe_relative(name)
            if (name not in allowed or type(entry) is not dict or set(entry) != {"size", "sha256"}
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or type(entry["sha256"]) is not str or not DIGEST.fullmatch(entry["sha256"])):
                raise InstallerError("installer_state_invalid")
        seen = set()
        for action in transaction["actions"]:
            if type(action) is not dict or set(action) != {"kind", "path"}:
                raise InstallerError("installer_state_invalid")
            key = (action["kind"], action["path"])
            if key in seen or action["kind"] not in ("withdraw", "publish"):
                raise InstallerError("installer_state_invalid")
            seen.add(key)
            safe_relative(action["path"])
            if action["path"] not in (transaction["original_files"] if action["kind"] == "withdraw" else new.by_path):
                raise InstallerError("installer_state_invalid")
        expected_actions = []
        originals = transaction["original_files"]
        if LAUNCHER in originals:
            expected_actions.append({"kind": "withdraw", "path": LAUNCHER})
        for name in sorted((set(originals) | set(new.by_path)) - {LAUNCHER}):
            if name in originals:
                expected_actions.append({"kind": "withdraw", "path": name})
            if name in new.by_path:
                expected_actions.append({"kind": "publish", "path": name})
        expected_actions.append({"kind": "publish", "path": LAUNCHER})
        if (transaction["actions"] != expected_actions[:len(transaction["actions"])]
                or len(transaction["actions"]) > len(expected_actions)
                or (transaction["actions"] and not transaction["backup_verified"])
                or (transaction["phase"] == "preparing" and transaction["actions"])
                or (transaction["phase"] in ("committing", "committed") and transaction["actions"] != expected_actions)):
            raise InstallerError("installer_state_invalid")
        allowed_directories = {parent.as_posix() for name in new.by_path for parent in Path(name).parents
                               if parent.as_posix() != "."}
        created = transaction["created_directories"]
        if (any(type(name) is not str or name not in allowed_directories for name in created)
                or len(set(created)) != len(created)):
            raise InstallerError("installer_state_invalid")
        self._data_path(Path(transaction["data_dir"]))
        return new

    def _rollback(self, transaction):
        manifest = self._validate_transaction(transaction)
        transaction["phase"] = "rolling_back"
        self._save(transaction)
        work = self.state / transaction["transaction_id"]
        # Keep old launcher withdrawn until EVERY other program file is restored.
        old_launcher = None
        for action in reversed(transaction["actions"]):
            relative = action["path"]
            destination = self.root / relative
            if action["kind"] == "publish":
                if destination.exists():
                    old = transaction["original_files"].get(relative)
                    if old is not None and not (work / "old" / relative).exists() and _matches(destination, old):
                        continue  # Already restored, including byte-identical repair files.
                    if not _matches(destination, manifest.by_path[relative]):
                        # Already-restored old files are allowed on a second recovery.
                        old = transaction["original_files"].get(relative)
                        if old is not None and _matches(destination, old) and not (work / "old" / relative).exists():
                            continue
                        raise InstallerError("installer_rollback_conflict")
                    discarded = work / "discard" / relative
                    platform.assert_safe_path(discarded)
                    if discarded.exists():
                        # A previous rollback already withdrew this new file.
                        raise InstallerError("installer_rollback_conflict")
                    discarded.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(destination, discarded)
            elif relative == LAUNCHER:
                old_launcher = action
            else:
                self._restore_old(transaction, relative)
            self._checkpoint("rollback_action", relative)
        previous = transaction["previous_receipt"]
        if previous is not None:
            _atomic_json(self.receipt_path, previous)
        elif self.receipt_path.exists():
            current = _read_json(self.receipt_path)
            if current.get("transaction_id") != transaction["transaction_id"]:
                raise InstallerError("installer_rollback_conflict")
            self.receipt_path.unlink()
        if old_launcher:
            self._restore_old(transaction, LAUNCHER)
        for relative, entry in transaction["original_files"].items():
            if not _matches(self.root / relative, entry):
                raise InstallerError("installer_rollback_verify_failed")
        for relative in sorted(transaction["created_directories"], key=lambda name: name.count("/"), reverse=True):
            directory = platform.assert_safe_path(self.root / relative)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()  # Only empty directories created by this transaction.
        self._checkpoint("rollback_complete")
        self.marker.unlink()
        try:
            _remove_tree(work)
        except (OSError, ValueError, RuntimeError):
            pass

    def _restore_old(self, transaction, relative):
        source = self.state / transaction["transaction_id"] / "old" / relative
        destination = self.root / relative
        expected = transaction["original_files"][relative]
        platform.assert_safe_path(source)
        platform.assert_safe_path(destination)
        if source.exists():
            if destination.exists() or not _matches(source, expected):
                raise InstallerError("installer_rollback_conflict")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        elif not _matches(destination, expected):
            raise InstallerError("installer_rollback_conflict")

    @_public
    def recover(self) -> RecoveryResult:
        platform.assert_safe_path(self.marker)
        if not self.marker.exists():
            return RecoveryResult(False, "none")
        transaction = _read_json(self.marker)
        manifest = self._validate_transaction(transaction)
        backup = self.root / BACKUPS / transaction["transaction_id"]
        with platform.SetupGuard(self.root, Path(transaction["data_dir"]), [self.root / entry.path for entry in manifest.files],
                                 setup_path=self.setup_path):
            if not self.marker.exists():
                return RecoveryResult(False, "none")
            locked_transaction = _read_json(self.marker)
            if locked_transaction != transaction:
                raise InstallerError("installer_state_changed")
            self._validate_transaction(locked_transaction)
            if transaction["phase"] == "committed":
                receipt = self._receipt()
                if (receipt is None or receipt["transaction_id"] != transaction["transaction_id"]
                        or receipt["manifest"] != transaction["manifest"]):
                    raise InstallerError("installer_recovery_failed")
                if any(not _matches(self.root / entry.path, entry) for entry in manifest.files):
                    raise InstallerError("installer_recovery_failed")
                self.marker.unlink()
                outcome = "committed"
            else:
                try:
                    self._rollback(transaction)
                except Exception:
                    raise InstallerError("installer_rollback_failed", backup_path=backup if backup.exists() else None) from None
                outcome = "rolled_back"
            try:
                _remove_tree(self.state / transaction["transaction_id"])
            except (OSError, ValueError, RuntimeError):
                pass
            return RecoveryResult(True, outcome, backup if backup.exists() else None)

    @_public
    def delete_backup(self, result: InstallResult) -> None:
        if (not isinstance(result, InstallResult) or result._owner != self._owner
                or self._results.get(result.transaction_id) is not result
                or result.backup_path != self.root / BACKUPS / result.transaction_id):
            raise InstallerError("installer_backup_identity")
        with platform.SetupGuard(self.root, result.data_dir, [], setup_path=self.setup_path):
            if self.marker.exists():
                raise InstallerError("installer_recovery_required")
            document = _read_json(result.backup_path / "backup.json")
            if document.get("transaction_id") != result.transaction_id or document.get("root") != str(self.root):
                raise InstallerError("installer_backup_identity")
            _remove_tree(result.backup_path)
            self._results.pop(result.transaction_id, None)
