"""Strict, versioned local configuration; no implicit defaults for credentials."""
from __future__ import annotations

import ipaddress
import json
import math
import ntpath
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z", re.ASCII)
PositiveInt = Annotated[int, Field(gt=0)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ConfigError(ValueError):
    """Sanitized configuration error, suitable for local diagnostics."""


def parse_json_strict(data: str | bytes) -> object:
    """Reject duplicate keys, non-finite numbers and non-Unicode scalar strings."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConfigError("json_duplicate_key")
            result[key] = value
        return result

    def bad_constant(_):
        raise ConfigError("json_nonfinite")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ConfigError("json_nonfinite")
        return parsed

    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="strict")
        if not isinstance(data, str):
            raise ConfigError("json_invalid")
        result = json.loads(data, object_pairs_hook=pairs,
                            parse_constant=bad_constant, parse_float=finite_float)
        stack = [result]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                item.encode("utf-8", errors="strict")
            elif isinstance(item, dict):
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return result
    except ConfigError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise ConfigError("json_invalid") from None


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid_identifier")
    return value


def validate_template_path(value: str) -> str:
    # Validate both Windows and POSIX syntax, irrespective of the running OS.
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("invalid_template_path")
    parts = value.split("/")
    if (len(parts) < 2 or parts[0] != "workflows"
            or any(p in ("", ".", "..") or ":" in p or p.endswith((" ", ".")) for p in parts)
            or PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
            or ntpath.isreserved(value)):
        raise ValueError("invalid_template_path")
    if not value.endswith(".json"):
        raise ValueError("invalid_template_path")
    return value


def confined_path(data_dir: Path, relative: str) -> Path:
    """Resolve all existing symlinks/junctions and fail closed outside data_dir."""
    root = Path(data_dir).resolve()
    path = root.joinpath(relative)
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        raise ConfigError("path_outside_data_dir") from None
    if resolved == root:
        raise ConfigError("path_outside_data_dir")
    return path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True,
                              revalidate_instances="always", hide_input_in_errors=True)


class Binding(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    field: str = Field(min_length=1, max_length=128)


class ServerConfig(StrictModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=18388, ge=1, le=65535)
    api_key: str = Field(min_length=1, repr=False)

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("invalid_api_key")
        return value

    @field_validator("listen_host")
    @classmethod
    def valid_host(cls, value):
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if len(value) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                                           for p in value.split(".")):
                raise ValueError("invalid_listen_host") from None
        return value


class ComfyConfig(StrictModel):
    base_url: str = "http://127.0.0.1:8188"

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        try:
            parts = urlsplit(value)
            port = parts.port
            if (parts.scheme not in ("http", "https") or not parts.hostname
                    or parts.username is not None or parts.password is not None
                    or "?" in value or "#" in value or "\\" in value
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in value)
                    or parts.netloc.endswith(":")
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
            ServerConfig.valid_host(parts.hostname)
        except ValueError:
            raise ValueError("invalid_comfyui_url") from None
        return value.rstrip("/")


class LimitsConfig(StrictModel):
    max_input_chars: PositiveInt = 4096
    max_body_bytes: PositiveInt = 65536
    max_inflight: PositiveInt = 1
    max_queue: int = Field(default=8, ge=0)
    request_timeout: PositiveFloat = 300.0
    connect_timeout: PositiveFloat = 3.0
    query_timeout: PositiveFloat = 3.0
    submit_timeout: PositiveFloat = 15.0
    download_timeout: PositiveFloat = 60.0
    transcode_timeout: PositiveFloat = 60.0
    poll_interval: PositiveFloat = 1.0
    probe_interval: PositiveFloat = 5.0
    max_audio_bytes: PositiveInt = 134217728
    max_audio_seconds: PositiveFloat = 600.0


class LoggingConfig(StrictModel):
    max_bytes: PositiveInt = 10485760
    backup_count: int = Field(default=5, ge=0)


class IdleReleaseConfig(StrictModel):
    seconds: int = Field(default=300, ge=1, le=86400)
    method: Literal["free", "workflow"]
    text: str = Field(default="好。", min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def valid_cleanup_text(cls, value):
        if (not value.strip() or any(0xD800 <= ord(char) <= 0xDFFF
                or (ord(char) < 32 and char not in "\t\r\n")
                or 0x7F <= ord(char) <= 0x9F for char in value)):
            raise ValueError("invalid_idle_release_text")
        return value


class ModelConfig(StrictModel):
    id: str
    cache_policy: Literal["inherit", "keep", "release", "idle_release"] = "inherit"
    idle_release: IdleReleaseConfig | None = None
    _id = field_validator("id")(validate_identifier)

    @model_validator(mode="after")
    def consistent_idle_release(self):
        if self.cache_policy == "idle_release" and self.idle_release is None:
            raise ValueError("idle_release_settings_required")
        if self.cache_policy != "idle_release" and self.idle_release is not None:
            raise ValueError("idle_release_settings_not_applicable")
        return self

    @model_serializer(mode="wrap")
    def legacy_compatible_dump(self, handler):
        # The 0.2 model schema forbids extra fields. Switching back to an old
        # policy must restore its exact representation, not emit a new null key.
        result = handler(self)
        if self.idle_release is None:
            result.pop("idle_release", None)
        return result


class PresetConfig(StrictModel):
    id: str
    model: str
    voice: str
    template: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_binding: Binding
    audio_output_node: str = Field(min_length=1, max_length=128)
    cache_binding: Binding | None = None

    _identifiers = field_validator("model", "voice")(validate_identifier)
    _template = field_validator("template")(validate_template_path)

    @field_validator("id")
    @classmethod
    def canonical_id(cls, value):
        try:
            if str(UUID(value)) != value:
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("invalid_preset_id") from None
        return value


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    server: ServerConfig
    comfyui: ComfyConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    models: list[ModelConfig] = Field(default_factory=list)
    presets: list[PresetConfig] = Field(default_factory=list)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("invalid_schema_version")
        return value

    @model_validator(mode="after")
    def unique_routes(self):
        models = {model.id: model for model in self.models}
        if len(models) != len(self.models):
            raise ValueError("duplicate_model")
        routes, ids = set(), set()
        for preset in self.presets:
            route = (preset.model, preset.voice)
            if route in routes or preset.id in ids:
                raise ValueError("duplicate_preset")
            if preset.model not in models:
                raise ValueError("unknown_preset_model")
            model = models[preset.model]
            needs_cache_binding = model.cache_policy in ("keep", "release") or (
                model.cache_policy == "idle_release" and model.idle_release is not None
                and model.idle_release.method == "workflow"
            )
            if needs_cache_binding and preset.cache_binding is None:
                raise ValueError("cache_binding_required")
            routes.add(route)
            ids.add(preset.id)
        return self


def load_config(data_dir: Path) -> AppConfig:
    path = confined_path(data_dir, "config.json")
    return AppConfig.model_validate(parse_json_strict(path.read_bytes()))


def _staged_file(parent: Path, content: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".comfybridge-", suffix=".tmp", dir=parent)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def save_config(data_dir: Path, config: AppConfig) -> None:
    """Atomic primary replacement with previous-version backup and failure rollback.

    Callers must hold the desktop configuration writer lock when sharing a directory.
    Neither live services nor this function hot-reload the configuration.
    """
    validated = AppConfig.model_validate(config.model_dump(mode="python"))
    content = (json.dumps(validated.model_dump(mode="json"), ensure_ascii=False,
                          indent=2, allow_nan=False) + "\n").encode("utf-8")
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = confined_path(root, "config.json")
    backup = confined_path(root, "config.json.bak")
    # Do not follow an existing in-root link either: replacement must affect this file.
    if target.is_symlink() or backup.is_symlink():
        raise ConfigError("config_symlink")
    previous = target.read_bytes() if target.exists() else None
    old_backup = backup.read_bytes() if backup.exists() else None
    staged: list[Path] = []
    backup_changed = False
    try:
        new_file = _staged_file(root, content)
        staged.append(new_file)
        rollback = None
        if previous is not None:
            previous_file = _staged_file(root, previous)
            staged.append(previous_file)
            if old_backup is not None:
                rollback = _staged_file(root, old_backup)
                staged.append(rollback)
            os.replace(previous_file, backup)
            backup_changed = True
        try:
            os.replace(new_file, target)
        except BaseException:
            if backup_changed:
                if rollback is not None:
                    os.replace(rollback, backup)
                else:
                    backup.unlink(missing_ok=True)
            raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)
