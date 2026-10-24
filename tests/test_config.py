from copy import deepcopy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from comfybridge.config import (
    AppConfig, Binding, ComfyConfig, ConfigError, IdleReleaseConfig, LimitsConfig, LoggingConfig,
    ModelConfig, PresetConfig, ServerConfig, load_config, parse_json_strict, save_config,
)


def config(**changes):
    values = {"server": {"api_key": "test-only-key"}, "comfyui": {}}
    values.update(changes)
    return AppConfig.model_validate(values)


def preset(**changes):
    values = dict(id=str(uuid4()), model="Index-TTS", voice="Example", template="workflows/example.json",
                  sha256="a" * 64, text_binding={"node_id": "2", "field": "text"}, audio_output_node="4")
    values.update(changes)
    return values


@pytest.mark.parametrize("value", ['{"a":1,"a":2}', '{"a":{"b":1,"b":2}}',
                                     'NaN', 'Infinity', '-Infinity', '1e999',
                                     '"\\ud800"', '"\\udfff"', '{"\\ud800":0}',
                                     b'"\xff"', b'\xff\xfe{}', '\ufeff{}', '{} trailing',
                                     '9' * 5000])
def test_strict_json_rejects(value):
    with pytest.raises(ValueError):
        parse_json_strict(value)


def test_strict_json_preserves_unicode_and_valid_types():
    assert parse_json_strict(b'{"emoji":"\\ud83d\\ude00","x":[1,true,null]}') == {"emoji": "😀", "x": [1, True, None]}


def test_defaults_and_no_default_credential():
    cfg = config()
    assert cfg.schema_version == 1
    assert cfg.limits.max_input_chars == 4096
    assert cfg.limits.max_queue == 8
    assert cfg.server.listen_host == "127.0.0.1"
    assert cfg.server.listen_port == 18388
    assert cfg.models == cfg.presets == []
    assert "test-only-key" not in repr(cfg)
    with pytest.raises(ValidationError):
        ServerConfig()


@pytest.mark.parametrize("key", ["", " ", " x", "x y", "x\r\n", "密钥", None, 123])
def test_keys_invalid(key):
    with pytest.raises(ValidationError):
        ServerConfig(api_key=key)


@pytest.mark.parametrize("model,values", [
    (ServerConfig, {"api_key": "key", "listen_port": True}),
    (ServerConfig, {"api_key": "key", "listen_port": "18388"}),
    (ServerConfig, {"api_key": "key", "listen_port": 65536}),
    (ServerConfig, {"api_key": "key", "listen_host": "http://127.0.0.1"}),
    (LimitsConfig, {"max_queue": -1}), (LimitsConfig, {"max_inflight": 0}),
    (LimitsConfig, {"max_input_chars": False}), (LimitsConfig, {"request_timeout": True}),
    (LimitsConfig, {"request_timeout": float("inf")}),
    (LimitsConfig, {"max_audio_seconds": float("nan")}),
    (LimitsConfig, {"connect_timeout": "3"}), (LoggingConfig, {"max_bytes": 0}),
    (LoggingConfig, {"backup_count": -1}),
    (Binding, {"node_id": 2, "field": "text"}),
    (Binding, {"node_id": "2", "field": "text", "unknown": 1}),
    (ModelConfig, {"id": "model", "cache_policy": "other"}),
])
def test_strict_models(model, values):
    with pytest.raises(ValidationError):
        model.model_validate(values)


@pytest.mark.parametrize("url", ["http://user@host", "https://u:p@host/", "file:///a", "host:8188",
                                  "http://host?", "http://host#", "http://host?a=b", "http://host/#a",
                                  "http://host:99999", "http://host:", "http://", "http://bad host",
                                  "http://host\\path", "http://a/%zz\n", "http://a%2eb/"])
def test_invalid_urls(url):
    with pytest.raises(ValidationError):
        ComfyConfig(base_url=url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8188", "https://comfy.example/api", "http://[::1]:8188"])
def test_valid_urls(url):
    assert ComfyConfig(base_url=url + "/").base_url == url


@pytest.mark.parametrize("identifier", ["", "../x", "a/b", "a\\b", "hello world", "声线", "-name", "x" * 65, 42])
def test_identifier_invalid(identifier):
    with pytest.raises(ValidationError):
        ModelConfig(id=identifier)


@pytest.mark.parametrize("path", ["../a.json", "a.json", "/workflows/a.json", "workflows/../a.json",
                                  "workflows//a.json", "workflows/./a.json", "workflows/a.json:stream",
                                  "workflows\\a.json", "C:/workflows/a.json", "workflows/a.txt",
                                  "workflows/a.json ", "workflows/NUL.json"])
def test_template_paths_invalid(path):
    with pytest.raises(ValidationError):
        PresetConfig.model_validate(preset(template=path))


@pytest.mark.parametrize("value", [True, 1.0, "1", 2])
def test_schema_version_exact(value):
    with pytest.raises(ValidationError):
        config(schema_version=value)


def test_unknown_fields_and_null():
    for changes in ({"unknown": 1}, {"models": None}, {"limits": None}):
        with pytest.raises(ValidationError):
            config(**changes)


def test_unique_routes_ids_and_registered_models():
    p = preset()
    for models, presets in [
        ([{"id": "Index-TTS"}, {"id": "Index-TTS"}], []),
        ([], [p]),
        ([{"id": "Index-TTS"}], [p, {**p, "id": str(uuid4())}]),
        ([{"id": "Index-TTS"}], [p, {**p, "voice": "other"}]),
    ]:
        with pytest.raises(ValidationError):
            config(models=models, presets=presets)
    assert len(config(models=[{"id": "Index-TTS"}, {"id": "index-tts"}]).models) == 2


@pytest.mark.parametrize("method", ["free", "workflow"])
def test_idle_release_defaults_and_schema_one_roundtrip(tmp_path, method):
    value = IdleReleaseConfig(method=method)
    assert value.seconds == 300 and value.text == "好。"
    cfg = config(models=[{"id": "Index-TTS", "cache_policy": "idle_release", "idle_release": {"method": method}}])
    assert cfg.schema_version == 1
    save_config(tmp_path, cfg)
    restored = load_config(tmp_path)
    assert restored == cfg
    assert restored.models[0].idle_release.method == method


@pytest.mark.parametrize("seconds", [0, -1, 86401, 2**64, True, False, "300", 1.0, None])
def test_idle_seconds_strict_and_bounded(seconds):
    with pytest.raises(ValidationError):
        IdleReleaseConfig(method="free", seconds=seconds)


@pytest.mark.parametrize("seconds", [1, 300, 86400])
def test_idle_seconds_boundary(seconds):
    assert IdleReleaseConfig(method="free", seconds=seconds).seconds == seconds


@pytest.mark.parametrize("text", ["", " ", "\t\n\r", "x" * 65, "😀" * 65, None, 1, True,
                                  "\ud800", "x\udfff", "\x00hello", "\x1b[31mhello", "x\x85y"])
def test_idle_text_strict_scalar_nonblank_and_bounded(text):
    with pytest.raises(ValidationError):
        IdleReleaseConfig(method="workflow", text=text)


@pytest.mark.parametrize("text", ["好。", " x ", "\nhello\t", "😀" * 64, "中" * 64])
def test_idle_text_retained_without_trimming(text):
    assert IdleReleaseConfig(method="workflow", text=text).text == text


@pytest.mark.parametrize("fields", [{}, {"method": None}, {"method": "other"}, {"method": True},
                                    {"method": "free", "unexpected": 1}])
def test_idle_method_required_strict_and_extra_forbidden(fields):
    with pytest.raises(ValidationError):
        IdleReleaseConfig.model_validate(fields)


@pytest.mark.parametrize("settings", [None, {}])
def test_idle_policy_requires_settings(settings):
    with pytest.raises(ValidationError):
        ModelConfig(id="m", cache_policy="idle_release", idle_release=settings)


@pytest.mark.parametrize("policy", ["inherit", "keep", "release"])
def test_legacy_policy_rejects_dangling_idle_settings(policy):
    with pytest.raises(ValidationError, match="idle_release_settings_not_applicable"):
        ModelConfig(id="m", cache_policy=policy, idle_release=IdleReleaseConfig(method="free"))
    assert ModelConfig(id="m", cache_policy=policy).idle_release is None


@pytest.mark.parametrize("policy", ["inherit", "keep", "release"])
def test_old_model_serialization_omits_only_idle_none(policy):
    model = ModelConfig(id="m", cache_policy=policy, idle_release=None)
    expected = {"id": "m", "cache_policy": policy}
    assert model.model_dump() == expected
    assert model.model_dump(mode="json") == expected
    assert json.loads(model.model_dump_json()) == expected
    assert model.model_dump(include={"id"}) == {"id": "m"}
    assert model.model_dump(exclude={"id"}) == {"cache_policy": policy}


@pytest.mark.parametrize("method", ["free", "workflow"])
def test_active_idle_settings_remain_serialized(method):
    model = ModelConfig(id="m", cache_policy="idle_release", idle_release=IdleReleaseConfig(method=method))
    expected = {"seconds": 300, "method": method, "text": "好。"}
    assert model.model_dump()["idle_release"] == expected
    assert json.loads(model.model_dump_json())["idle_release"] == expected
    assert model.model_dump(include={"idle_release"}) == {"idle_release": expected}


@pytest.mark.parametrize("policy", ["inherit", "keep", "release"])
def test_switch_back_saved_models_are_readable_by_old_extra_forbid_schema(tmp_path, policy):
    from typing import Literal
    from pydantic import BaseModel, ConfigDict

    class LegacyModel(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        id: str
        cache_policy: Literal["inherit", "keep", "release"] = "inherit"

    cfg = config(models=[{"id": "m", "cache_policy": "idle_release", "idle_release": {"method": "free"}}])
    save_config(tmp_path, cfg)
    assert "idle_release" in json.loads((tmp_path / "config.json").read_bytes())["models"][0]
    cfg.models[0] = ModelConfig(id="m", cache_policy=policy)
    save_config(tmp_path, cfg)
    saved = json.loads((tmp_path / "config.json").read_bytes())
    assert "idle_release" not in saved["models"][0]
    assert LegacyModel.model_validate(saved["models"][0]).cache_policy == policy
    assert load_config(tmp_path).models[0].idle_release is None


def test_old_config_digest_and_other_nullable_fields_are_unchanged():
    import hashlib
    from comfybridge.control import config_digest

    cfg = config(models=[{"id": "Index-TTS", "cache_policy": "inherit"}], presets=[preset()])
    expected = cfg.model_dump(mode="json")
    expected["models"] = [{"id": "Index-TTS", "cache_policy": "inherit"}]
    assert cfg.model_dump(mode="json") == expected
    assert "cache_binding" in expected["presets"][0] and expected["presets"][0]["cache_binding"] is None
    legacy_digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert config_digest(cfg) == legacy_digest


def test_idle_free_needs_no_cache_binding_but_workflow_does():
    values = {"id": "Index-TTS", "cache_policy": "idle_release", "idle_release": {"method": "free"}}
    assert config(models=[values], presets=[preset()]).presets[0].cache_binding is None
    values["idle_release"]["method"] = "workflow"
    with pytest.raises(ValidationError, match="cache_binding_required"):
        config(models=[values], presets=[preset()])
    config(models=[values], presets=[preset(cache_binding={"node_id": "3", "field": "keep_models_cached"})])


def test_old_schema_one_config_loads_without_rewrite(tmp_path):
    document = {"schema_version": 1, "server": {"api_key": "test-key"}, "comfyui": {},
                "models": [{"id": "m", "cache_policy": "keep"}]}
    encoded = json.dumps(document).encode("utf-8")
    (tmp_path / "config.json").write_bytes(encoded)
    restored = load_config(tmp_path)
    assert restored.schema_version == 1
    assert restored.models[0].cache_policy == "keep" and restored.models[0].idle_release is None
    assert (tmp_path / "config.json").read_bytes() == encoded


def test_mutated_idle_settings_fail_before_save(tmp_path):
    cfg = config(models=[{"id": "m", "cache_policy": "idle_release", "idle_release": {"method": "free"}}])
    save_config(tmp_path, cfg)
    original = (tmp_path / "config.json").read_bytes()
    cfg.models[0] = cfg.models[0].model_copy(update={"cache_policy": "inherit"})
    with pytest.raises(ValidationError):
        save_config(tmp_path, cfg)
    assert (tmp_path / "config.json").read_bytes() == original


def test_cache_override_requires_binding():
    with pytest.raises(ValidationError):
        config(models=[{"id": "Index-TTS", "cache_policy": "keep"}], presets=[preset()])
    config(models=[{"id": "Index-TTS", "cache_policy": "keep"}],
           presets=[preset(cache_binding={"node_id": "3", "field": "keep_models_cached"})])


@pytest.mark.parametrize("identifier", ["not-uuid", str(uuid4()).upper(), uuid4(), "{" + str(uuid4()) + "}"])
def test_canonical_preset_id(identifier):
    with pytest.raises(ValidationError):
        PresetConfig.model_validate(preset(id=identifier))


def test_save_load_backup_and_no_hot_reload(tmp_path):
    first = config()
    save_config(tmp_path, first)
    original = (tmp_path / "config.json").read_bytes()
    assert not (tmp_path / "config.json.bak").exists()
    second = config(server={"api_key": "different-key", "listen_port": 18389})
    save_config(tmp_path, second)
    assert (tmp_path / "config.json.bak").read_bytes() == original
    assert load_config(tmp_path) == second
    assert first.server.listen_port == 18388
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


@pytest.mark.parametrize("existing_backup", [False, True])
def test_failed_primary_replace_rolls_back_backup(tmp_path, monkeypatch, existing_backup):
    save_config(tmp_path, config())
    old_main = (tmp_path / "config.json").read_bytes()
    backup = tmp_path / "config.json.bak"
    if existing_backup:
        backup.write_bytes(b"older backup")
    real_replace = os.replace

    def fail_primary(source, destination):
        if Path(destination).name == "config.json":
            raise OSError("simulated primary replace failure")
        real_replace(source, destination)

    monkeypatch.setattr("comfybridge.config.os.replace", fail_primary)
    with pytest.raises(OSError):
        save_config(tmp_path, config(server={"api_key": "new-key"}))
    assert (tmp_path / "config.json").read_bytes() == old_main
    assert backup.read_bytes() == b"older backup" if existing_backup else not backup.exists()
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


def test_failed_backup_replace_does_not_touch_primary(tmp_path, monkeypatch):
    save_config(tmp_path, config())
    original = (tmp_path / "config.json").read_bytes()
    monkeypatch.setattr("comfybridge.config.os.replace", lambda *_: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        save_config(tmp_path, config(server={"api_key": "new-key"}))
    assert (tmp_path / "config.json").read_bytes() == original
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


def test_fsync_failure_leaves_existing_config_untouched(tmp_path, monkeypatch):
    save_config(tmp_path, config())
    original = (tmp_path / "config.json").read_bytes()
    monkeypatch.setattr("comfybridge.config.os.fsync", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        save_config(tmp_path, config(server={"api_key": "new-key"}))
    assert (tmp_path / "config.json").read_bytes() == original
    assert not (tmp_path / "config.json.bak").exists()
    assert not list(tmp_path.glob(".comfybridge-*.tmp"))


def test_invalid_mutated_config_not_saved(tmp_path):
    cfg = config()
    cfg.models.extend([ModelConfig(id="m"), ModelConfig(id="m")])
    with pytest.raises(ValidationError):
        save_config(tmp_path, cfg)
    assert not (tmp_path / "config.json").exists()


def test_load_duplicate_keys_rejected(tmp_path):
    (tmp_path / "config.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ConfigError, match="json_duplicate_key"):
        load_config(tmp_path)


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc.winerror if hasattr(exc, 'winerror') else exc.errno}")


@pytest.mark.parametrize("name", ["config.json", "config.json.bak"])
def test_save_does_not_follow_external_symlink(tmp_path, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = outside / "secret.json"
    destination.write_bytes(b"DO NOT OVERWRITE")
    data = tmp_path / "data"
    data.mkdir()
    symlink_or_skip(data / name, destination)
    with pytest.raises(ConfigError):
        save_config(data, config())
    assert destination.read_bytes() == b"DO NOT OVERWRITE"


def test_load_rejects_external_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(config().model_dump_json())
    data = tmp_path / "data"
    data.mkdir()
    symlink_or_skip(data / "config.json", outside)
    with pytest.raises(ConfigError):
        load_config(data)
