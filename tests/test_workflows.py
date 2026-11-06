from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from comfybridge.config import AppConfig, ConfigError, IdleReleaseConfig, parse_json_strict
from comfybridge.workflows import (
    LoadedPreset, MAX_CLEANUP_SEED, WorkflowError, convert_ui_workflow, import_template, load_presets, validate_live,
)


@pytest.fixture
def graph():
    fixture = Path(__file__).resolve().parents[1] / "examples/workflows/index-tts-2.5_example.json"
    return parse_json_strict(fixture.read_bytes())


@pytest.fixture
def schemas():
    return {
        "LoadAudio": {"input": {"required": {"audio": [["example.wav"]]}}, "output": ["AUDIO"], "output_node": False},
        "IndexTTS25BaseNode": {
            "input": {
                "required": {"text": ["STRING", {}], "reference_audio": ["AUDIO"],
                             "lang": [["ZH", "EN"]], "duration_factor": ["FLOAT", {"min": .5, "max": 2}]},
                "optional": {
                    "do_sample_mode": [["on", "off"]], "temperature": ["FLOAT"], "top_p": ["FLOAT"],
                    "top_k": ["INT"], "num_beams": ["INT"], "repetition_penalty": ["FLOAT"],
                    "length_penalty": ["FLOAT"], "max_mel_tokens": ["INT"], "max_tokens_per_sentence": ["INT"],
                    "interval_silence_ms": ["INT"], "text_normalization": ["BOOLEAN"], "seed": ["INT"],
                    "cache_control": ["DICT"],
                },
            }, "output": ["AUDIO", "INT", "STRING"], "output_node": False,
        },
        "IndexTTS25CacheControlNode": {
            "input": {"required": {"keep_models_cached": ["BOOLEAN"]}}, "output": ["DICT"], "output_node": False,
        },
        "SaveAudio": {"input": {"required": {"audio": ["AUDIO"], "filename_prefix": ["STRING"]}},
                      "output": [], "output_node": True},
    }


def register(tmp_path, graph, *, policy="inherit", cache=True, idle_release=None, **preset_overrides):
    identifier = str(uuid4())
    relative, digest = import_template(tmp_path, graph, "index-tts-2.5", "example", identifier)
    preset = dict(id=identifier, model="index-tts-2.5", voice="example", template=relative,
                  sha256=digest, text_binding={"node_id": "2", "field": "text"}, audio_output_node="4")
    if cache:
        preset["cache_binding"] = {"node_id": "3", "field": "keep_models_cached"}
    preset.update(preset_overrides)
    config = AppConfig.model_validate({"server": {"api_key": "test-only-key"}, "comfyui": {},
                                      "models": [{"id": "index-tts-2.5", "cache_policy": policy,
                                                  "idle_release": idle_release}], "presets": [preset]})
    return config


def load(tmp_path, graph, **kwargs):
    config = register(tmp_path, graph, **kwargs)
    return load_presets(config, tmp_path)[("index-tts-2.5", "example")]


def ui_document(graph):
    """Mirror the read-only supplied UI fixture's exact positional widget layout."""
    tts_names = ["text", "lang", "duration_factor", "do_sample_mode", "temperature", "top_p", "top_k",
                 "num_beams", "repetition_penalty", "length_penalty", "max_mel_tokens",
                 "max_tokens_per_sentence", "interval_silence_ms", "text_normalization", "seed"]
    return {
        "nodes": [
            {"id": 1, "type": "LoadAudio", "mode": 0, "inputs": [],
             "widgets_values": [graph["1"]["inputs"]["audio"], ""],
             "widgets_values_named": {**graph["1"]["inputs"], "upload": ""}},
            {"id": 2, "type": "IndexTTS25BaseNode", "mode": 0,
             "inputs": [{"name": "reference_audio", "type": "AUDIO", "link": 1},
                        {"name": "cache_control", "type": "DICT", "link": 2}],
             "widgets_values": [graph["2"]["inputs"][field] for field in tts_names] + ["randomize"],
             "widgets_values_named": {**{field: graph["2"]["inputs"][field] for field in tts_names},
                                      "control_after_generate": "randomize"}},
            {"id": 3, "type": "IndexTTS25CacheControlNode", "mode": 0, "inputs": [],
             "widgets_values": [False], "widgets_values_named": {"keep_models_cached": False}},
            {"id": 4, "type": "SaveAudio", "mode": 0,
             "inputs": [{"name": "audio", "type": "AUDIO", "link": 3}],
             "widgets_values": [graph["4"]["inputs"]["filename_prefix"]],
             "widgets_values_named": deepcopy({"filename_prefix": graph["4"]["inputs"]["filename_prefix"]})},
        ],
        "links": [[1, 1, 0, 2, 0, "AUDIO"], [2, 3, 0, 2, 1, "DICT"], [3, 2, 0, 4, 0, "AUDIO"]],
        "extra": {"groupNodes": {}}, "version": 0.4,
    }


def test_immutable_import_and_digest(tmp_path, graph):
    identifier = str(uuid4())
    relative, digest = import_template(tmp_path, graph, "a_b", "c", identifier)
    content = (tmp_path / relative).read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest
    assert digest in relative and identifier in relative
    assert import_template(tmp_path, graph, "a_b", "c", identifier) == (relative, digest)
    graph["2"]["inputs"]["text"] = "new template"
    second, second_digest = import_template(tmp_path, graph, "a_b", "c", identifier)
    assert second != relative and second_digest != digest
    assert (tmp_path / relative).read_bytes() == content
    assert not (tmp_path / "config.json").exists()
    assert not list((tmp_path / "workflows").glob("*.tmp"))


def test_immutable_collision_never_overwrites(tmp_path, graph):
    identifier = str(uuid4())
    relative, _ = import_template(tmp_path, graph, "m", "v", identifier)
    (tmp_path / relative).write_bytes(b"external tampering")
    with pytest.raises(WorkflowError, match="workflow_immutable_conflict"):
        import_template(tmp_path, graph, "m", "v", identifier)
    assert (tmp_path / relative).read_bytes() == b"external tampering"


@pytest.mark.parametrize("policy,expected", [("inherit", False), ("keep", True), ("release", False)])
def test_prompt_isolation_full_text_and_cache(tmp_path, graph, policy, expected):
    loaded = load(tmp_path, graph, policy=policy)
    original = deepcopy(loaded.graph)
    text = "  完整😀\n**markdown**  "
    first = loaded.build_prompt(text)
    second = loaded.build_prompt("second")
    assert first["2"]["inputs"]["text"] == text
    assert second["2"]["inputs"]["text"] == "second"
    assert first["3"]["inputs"]["keep_models_cached"] is expected
    first["1"]["inputs"]["audio"] = "changed"
    assert loaded.graph == original == graph
    assert second["1"]["inputs"]["audio"] != "changed"


def test_loaded_preset_three_positional_arguments_remain_compatible(tmp_path, graph):
    original = load(tmp_path, graph, policy="keep")
    loaded = LoadedPreset(original.preset, original.graph, "keep")
    assert loaded.idle_release is None
    assert loaded.build_prompt("hello")["3"]["inputs"]["keep_models_cached"] is True


@pytest.mark.parametrize("seed", [1, 2, MAX_CLEANUP_SEED])
def test_idle_workflow_normal_keep_cleanup_release_and_exact_seed(tmp_path, graph, seed):
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    original = deepcopy(loaded.graph)
    normal = loaded.build_prompt("  Full normal text 😀\n ")
    cleanup = loaded.build_cleanup_prompt("  好。  ", seed)
    assert normal["3"]["inputs"]["keep_models_cached"] is True
    assert normal["2"]["inputs"]["seed"] == original["2"]["inputs"]["seed"]
    assert normal["2"]["inputs"]["text"] == "  Full normal text 😀\n "
    expected = deepcopy(original)
    expected["2"]["inputs"].update(text="  好。  ", seed=seed)
    expected["3"]["inputs"]["keep_models_cached"] = False
    assert cleanup == expected
    assert loaded.graph == original == graph
    cleanup["1"]["inputs"]["audio"] = "modified caller copy"
    cleanup["2"]["inputs"]["cache_control"][0] = "changed"
    assert loaded.graph == original
    assert loaded.build_cleanup_prompt("好。", seed)["1"]["inputs"]["audio"] == original["1"]["inputs"]["audio"]


def test_cleanup_prunes_unselected_nodes_and_changes_only_selected_bindings(tmp_path, graph):
    graph["other"] = {"class_type": "OtherTTS", "inputs": {"text": "untouched", "seed": 123}}
    graph["other_output"] = {"class_type": "SaveAudio", "inputs": {"audio": ["other", 0]}}
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert set(loaded.build_cleanup_prompt("好。", 1)) == {"1", "2", "3", "4"}
    assert loaded.graph["other"]["inputs"] == {"text": "untouched", "seed": 123}
    first = loaded.build_cleanup_prompt("好。", 1)
    second = loaded.build_cleanup_prompt("好。", 2)
    assert first["2"]["inputs"]["seed"] == 1
    first["2"]["inputs"]["seed"] = 2
    assert first == second  # Durable seed is the only per-cleanup fingerprint change.


def test_loaded_idle_settings_are_a_snapshot(tmp_path, graph):
    cfg = register(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    loaded = load_presets(cfg, tmp_path)[("index-tts-2.5", "example")]
    assert isinstance(loaded.idle_release, IdleReleaseConfig)
    cfg.models[0].idle_release.seconds = 1
    cfg.models[0].idle_release.text = "changed"
    assert loaded.idle_release.seconds == 300 and loaded.idle_release.text == "好。"


@pytest.mark.parametrize("cache", [False, True])
def test_idle_free_normal_prompt_respects_only_explicit_binding(tmp_path, graph, cache):
    loaded = load(tmp_path, graph, policy="idle_release", cache=cache, idle_release={"method": "free"})
    expected = deepcopy(graph)
    expected["2"]["inputs"]["text"] = "new business request"
    if cache:
        expected["3"]["inputs"]["keep_models_cached"] = True
    assert loaded.build_prompt("new business request") == expected
    assert loaded.graph == graph
    with pytest.raises(WorkflowError, match="cache_cleanup_unsupported"):
        loaded.build_cleanup_prompt("好。", 1)


def test_idle_free_accepts_no_cache_node_and_unrelated_synthesis_class(tmp_path, graph, schemas):
    del graph["3"]
    del graph["2"]["inputs"]["cache_control"]
    del graph["2"]["inputs"]["seed"]
    graph["2"]["class_type"] = "OtherTTS"
    schemas["OtherTTS"] = schemas.pop("IndexTTS25BaseNode")
    del schemas["OtherTTS"]["input"]["optional"]["seed"]
    loaded = load(tmp_path, graph, policy="idle_release", cache=False, idle_release={"method": "free"})
    assert set(loaded.build_prompt("hello")) == {"1", "2", "4"}
    assert validate_live(loaded, schemas) == []


@pytest.mark.parametrize("seed", [0, -1, MAX_CLEANUP_SEED + 1, 2**64, True, False, 1.0, "1", None])
def test_cleanup_seed_rejects_zero_invalid_or_exhausted_sequence(tmp_path, graph, seed):
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    original = deepcopy(loaded.graph)
    with pytest.raises(WorkflowError, match="cache_cleanup_seed_invalid"):
        loaded.build_cleanup_prompt("好。", seed)
    assert loaded.graph == original


@pytest.mark.parametrize("text", ["", " \n", "x" * 65, "\ud800", "\x1b[31mx", None, 1, True])
def test_cleanup_text_is_validated_without_mutation(tmp_path, graph, text):
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    original = deepcopy(loaded.graph)
    with pytest.raises(WorkflowError, match="cache_cleanup_text"):
        loaded.build_cleanup_prompt(text, 1)
    assert loaded.graph == original


@pytest.mark.parametrize("policy", ["inherit", "keep", "release"])
def test_cleanup_never_enabled_for_old_strategies(tmp_path, graph, policy):
    loaded = load(tmp_path, graph, policy=policy)
    with pytest.raises(WorkflowError, match="cache_cleanup_unsupported"):
        loaded.build_cleanup_prompt("好。", 1)


@pytest.mark.parametrize("seed", [None, False, "1", -1, MAX_CLEANUP_SEED + 1, ["1", 0]])
def test_idle_workflow_rejects_nonliteral_or_out_of_range_template_seed(tmp_path, graph, seed):
    graph["2"]["inputs"]["seed"] = seed
    with pytest.raises(WorkflowError, match="cache_cleanup_seed_binding"):
        load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})


@pytest.mark.parametrize("seed", [0, MAX_CLEANUP_SEED])
def test_idle_workflow_accepts_static_uint32_seed_boundaries(tmp_path, graph, seed):
    graph["2"]["inputs"]["seed"] = seed
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert loaded.build_prompt("normal")["2"]["inputs"]["seed"] == seed
    assert loaded.build_cleanup_prompt("好。", 1)["2"]["inputs"]["seed"] == 1


@pytest.mark.parametrize("mutation", ["other_class", "other_text_field", "voice_class"])
def test_idle_workflow_only_supports_explicit_base_synthesis_text_binding(tmp_path, graph, mutation):
    binding = {"node_id": "2", "field": "text"}
    if mutation == "other_class":
        graph["2"]["class_type"] = "OtherTTS"
    elif mutation == "voice_class":
        graph["2"]["class_type"] = "IndexTTS25VoiceBaseNode"
    else:
        binding["field"] = "lang"
    with pytest.raises(WorkflowError, match="cache_cleanup_unsupported"):
        load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"}, text_binding=binding)


def test_idle_workflow_cache_binding_still_must_feed_selected_synthesis(tmp_path, graph):
    del graph["2"]["inputs"]["cache_control"]
    with pytest.raises(WorkflowError, match="workflow_cache_binding"):
        load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})


def test_optional_missing_seed_is_inserted_only_for_cleanup(tmp_path, graph, schemas):
    del graph["2"]["inputs"]["seed"]
    schemas["IndexTTS25BaseNode"]["input"]["optional"]["seed"] = ["INT", {"min": 0, "max": MAX_CLEANUP_SEED}]
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    original = deepcopy(loaded.graph)
    assert "seed" not in loaded.build_prompt("normal")["2"]["inputs"]
    assert loaded.build_cleanup_prompt("好。", 1)["2"]["inputs"]["seed"] == 1
    assert validate_live(loaded, schemas) == []
    assert loaded.graph == original


@pytest.mark.parametrize("seed_schema", [
    ["INT"], ["INT", {}], ["INT", {"min": 0, "max": MAX_CLEANUP_SEED}],
    ["INT", {"min": -1, "max": 2**64 - 1}],
])
def test_live_cleanup_seed_accepts_full_supported_range(tmp_path, graph, schemas, seed_schema):
    schemas["IndexTTS25BaseNode"]["input"]["optional"]["seed"] = seed_schema
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    before_graph, before_schemas = deepcopy(loaded.graph), deepcopy(schemas)
    assert validate_live(loaded, schemas) == []
    assert loaded.graph == before_graph and schemas == before_schemas


@pytest.mark.parametrize("seed_schema", [
    None, ["FLOAT"], ["STRING"], ["*"], [[0, 1, 2]], ["INT", None],
    ["INT", {"min": True}], ["INT", {"max": "4294967295"}], ["INT", {"max": float("inf")}],
])
def test_live_cleanup_seed_missing_wrong_type_or_malformed_fails_closed(tmp_path, graph, schemas, seed_schema):
    del graph["2"]["inputs"]["seed"]
    optional = schemas["IndexTTS25BaseNode"]["input"]["optional"]
    if seed_schema is None:
        del optional["seed"]
    else:
        optional["seed"] = seed_schema
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert "cache_cleanup_seed_schema" in validate_live(loaded, schemas)


@pytest.mark.parametrize("constraints", [{"min": 1}, {"max": MAX_CLEANUP_SEED - 1}, {"min": 0, "max": 100}])
def test_live_cleanup_requires_entire_seed_range(tmp_path, graph, schemas, constraints):
    schemas["IndexTTS25BaseNode"]["input"]["optional"]["seed"] = ["INT", constraints]
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert "cache_cleanup_seed_range" in validate_live(loaded, schemas)


def test_live_cleanup_accepts_required_seed_when_normal_template_supplies_it(tmp_path, graph, schemas):
    inputs = schemas["IndexTTS25BaseNode"]["input"]
    del inputs["optional"]["seed"]
    inputs["required"]["seed"] = ["INT", {"min": 0, "max": MAX_CLEANUP_SEED}]
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert validate_live(loaded, schemas) == []


def test_live_cleanup_rejects_ambiguous_seed_definition(tmp_path, graph, schemas):
    schemas["IndexTTS25BaseNode"]["input"]["required"]["seed"] = ["INT"]
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    assert "cache_cleanup_seed_schema" in validate_live(loaded, schemas)


def test_live_cleanup_text_error_remains_sanitized_diagnostic(tmp_path, graph, schemas):
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    invalid = loaded.idle_release.model_copy(update={"text": "PRIVATE" * 100})
    damaged = LoadedPreset(loaded.preset, loaded.graph, "idle_release", invalid)
    assert "cache_cleanup_text" in validate_live(damaged, schemas)


def test_live_cleanup_static_errors_remain_explicit_without_execution(tmp_path, graph, schemas):
    loaded = load(tmp_path, graph, policy="idle_release", idle_release={"method": "workflow"})
    damaged_graph = deepcopy(loaded.graph)
    damaged_graph["2"]["inputs"]["seed"] = True
    damaged = LoadedPreset(loaded.preset, damaged_graph, "idle_release", loaded.idle_release)
    assert validate_live(damaged, schemas) == ["cache_cleanup_seed_binding"]
    damaged_graph["2"]["inputs"]["seed"] = 1
    damaged_graph["2"]["class_type"] = "OtherTTS"
    assert validate_live(damaged, schemas) == ["cache_cleanup_unsupported"]


def test_old_strategies_retain_wider_normal_template_seed_support(tmp_path, graph, schemas):
    graph["2"]["inputs"]["seed"] = 2**64 - 1
    loaded = load(tmp_path, graph, policy="keep")
    assert loaded.build_prompt("normal")["2"]["inputs"]["seed"] == 2**64 - 1
    assert validate_live(loaded, schemas) == []


def test_legacy_seed_constraints_do_not_become_cleanup_constraints(tmp_path, graph, schemas):
    schemas["IndexTTS25BaseNode"]["input"]["optional"]["seed"] = ["INT", {"min": 100, "max": 500000000}]
    assert validate_live(load(tmp_path, graph, policy="keep"), schemas) == []
    assert validate_live(load(tmp_path, graph, policy="idle_release", idle_release={"method": "free"}), schemas) == []


def test_inherit_preserves_true_and_can_omit_cache(tmp_path, graph):
    graph["3"]["inputs"]["keep_models_cached"] = True
    assert load(tmp_path, graph).build_prompt("hello")["3"]["inputs"]["keep_models_cached"] is True
    del graph["2"]["inputs"]["cache_control"]
    del graph["3"]
    assert "3" not in load(tmp_path, graph, cache=False).build_prompt("hello")


def test_prunes_unselected_output_ancestors(tmp_path, graph):
    graph["5"] = {"class_type": "UnknownButDisconnected", "inputs": {"secret": "never submit"}}
    graph["6"] = {"class_type": "SaveAudio", "inputs": {"audio": ["5", 0], "filename_prefix": "unused"}}
    loaded = load(tmp_path, graph)
    assert set(loaded.build_prompt("hello")) == {"1", "2", "3", "4"}
    assert "5" in loaded.graph


def test_digest_tampering(tmp_path, graph):
    config = register(tmp_path, graph)
    path = tmp_path / config.presets[0].template
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(WorkflowError, match="workflow_digest_mismatch"):
        load_presets(config, tmp_path)


@pytest.mark.parametrize("binding", [{"node_id": "absent", "field": "text"},
                                      {"node_id": "2", "field": "reference_audio"},
                                      {"node_id": "2", "field": "missing"},
                                      {"node_id": "4", "field": "filename_prefix"},
                                      {"node_id": "disconnected", "field": "text"}])
def test_invalid_text_bindings(tmp_path, graph, binding):
    graph["disconnected"] = {"class_type": "Text", "inputs": {"text": "other"}}
    with pytest.raises(WorkflowError, match="workflow_text_binding"):
        load(tmp_path, graph, text_binding=binding)


@pytest.mark.parametrize("mutation", ["wrong_field", "wrong_node", "disconnected", "wrong_class", "not_bool", "wrong_slot"])
def test_cache_binding_must_feed_synthesis(tmp_path, graph, mutation):
    binding = {"node_id": "3", "field": "keep_models_cached"}
    if mutation == "wrong_field":
        binding["field"] = "text"
    elif mutation == "wrong_node":
        binding["node_id"] = "404"
    elif mutation == "disconnected":
        del graph["2"]["inputs"]["cache_control"]
    elif mutation == "wrong_class":
        graph["3"]["class_type"] = "FakeCache"
    elif mutation == "not_bool":
        graph["3"]["inputs"]["keep_models_cached"] = "false"
    else:
        graph["2"]["inputs"]["cache_control"][1] = 1
    with pytest.raises(WorkflowError, match="workflow_cache_binding"):
        load(tmp_path, graph, cache_binding=binding)


def upstream_text_graph(graph):
    """Text supplied by an upstream multiline string primitive linked into synthesis."""
    graph["9"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "原始文本"}}
    graph["2"]["inputs"]["text"] = ["9", 0]
    return graph


UPSTREAM_TEXT = {"node_id": "9", "field": "value"}


@pytest.mark.parametrize("policy,idle_release,expected", [
    ("keep", None, True), ("release", None, False), ("idle_release", {"method": "free"}, True),
])
def test_upstream_string_text_binding_accepts_cache_binding(tmp_path, graph, schemas, policy, idle_release, expected):
    upstream_text_graph(graph)
    loaded = load(tmp_path, graph, policy=policy, idle_release=idle_release, text_binding=UPSTREAM_TEXT)
    prompt = loaded.build_prompt("新的请求文本")
    assert prompt["9"]["inputs"]["value"] == "新的请求文本"
    assert prompt["2"]["inputs"]["text"] == ["9", 0]
    assert prompt["3"]["inputs"]["keep_models_cached"] is expected
    assert loaded.graph == graph
    schemas["PrimitiveStringMultiline"] = {
        "input": {"required": {"value": ["STRING", {"multiline": True}]}}, "output": ["STRING"], "output_node": False,
    }
    assert validate_live(loaded, schemas) == []


def test_upstream_string_text_binding_cache_must_feed_synthesis(tmp_path, graph):
    upstream_text_graph(graph)
    del graph["2"]["inputs"]["cache_control"]
    with pytest.raises(WorkflowError, match="workflow_cache_binding"):
        load(tmp_path, graph, policy="keep", text_binding=UPSTREAM_TEXT)


def test_cache_feeding_only_unselected_branch_is_rejected(tmp_path, graph):
    upstream_text_graph(graph)
    del graph["2"]["inputs"]["cache_control"]
    # The cache and the text feed another synthesis branch that never reaches node 4.
    graph["other"] = {"class_type": "IndexTTS25BaseNode",
                      "inputs": {"text": ["9", 0], "reference_audio": ["1", 0], "cache_control": ["3", 0]}}
    graph["other_output"] = {"class_type": "SaveAudio", "inputs": {"audio": ["other", 0]}}
    with pytest.raises(WorkflowError, match="workflow_cache_binding"):
        load(tmp_path, graph, policy="keep", text_binding=UPSTREAM_TEXT)


def test_cache_synthesis_must_receive_bound_text(tmp_path, graph):
    # The selected synthesis gets its cache control, but the bound text only reaches
    # an unrelated branch that does not feed the selected output.
    graph["9"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "x"}}
    graph["other"] = {"class_type": "IndexTTS25BaseNode", "inputs": {"text": ["9", 0], "reference_audio": ["1", 0]}}
    graph["other_output"] = {"class_type": "SaveAudio", "inputs": {"audio": ["other", 0]}}
    with pytest.raises(WorkflowError):
        load(tmp_path, graph, policy="keep", text_binding=UPSTREAM_TEXT)


CLEANUP_SYNTHESIS_CLASSES = ["IndexTTS25BaseNode", "IndexTTS25EmotionTextNode",
                             "IndexTTS25EmotionAudioNode", "IndexTTS25EmotionVectorNode"]
IDLE_WORKFLOW = {"policy": "idle_release", "idle_release": {"method": "workflow"}}


@pytest.mark.parametrize("synthesis_class", CLEANUP_SYNTHESIS_CLASSES)
@pytest.mark.parametrize("text_source", ["direct", "PrimitiveStringMultiline", "PrimitiveString"])
def test_idle_workflow_cleanup_supports_emotion_nodes_and_string_primitives(
        tmp_path, graph, schemas, synthesis_class, text_source):
    graph["2"]["class_type"] = synthesis_class
    schemas[synthesis_class] = deepcopy(schemas["IndexTTS25BaseNode"])
    binding = {"node_id": "2", "field": "text"}
    if text_source != "direct":
        upstream_text_graph(graph)
        graph["9"]["class_type"] = text_source
        schemas[text_source] = {"input": {"required": {"value": ["STRING", {}]}},
                                "output": ["STRING"], "output_node": False}
        binding = UPSTREAM_TEXT
    loaded = load(tmp_path, graph, **IDLE_WORKFLOW, text_binding=binding)
    original = deepcopy(loaded.graph)
    normal = loaded.build_prompt("业务文本")
    cleanup = loaded.build_cleanup_prompt("好。", 7)
    expected = deepcopy(original)
    expected[binding["node_id"]]["inputs"][binding["field"]] = "好。"
    expected["2"]["inputs"]["seed"] = 7
    expected["3"]["inputs"]["keep_models_cached"] = False
    assert cleanup == expected
    assert normal["2"]["inputs"]["seed"] == original["2"]["inputs"]["seed"]
    assert normal["3"]["inputs"]["keep_models_cached"] is True
    assert loaded.graph == original == graph
    assert validate_live(loaded, schemas) == []


def test_idle_workflow_cleanup_supports_attached_emotion_text_shape(tmp_path, graph):
    # Mirrors an exported Emotion Text API workflow with extra optional inputs.
    upstream_text_graph(graph)
    graph["2"]["class_type"] = "IndexTTS25EmotionTextNode"
    graph["2"]["inputs"].update(emotion_description="", emotion_weight=0.6, seed=3292620009)
    loaded = load(tmp_path, graph, **IDLE_WORKFLOW, text_binding=UPSTREAM_TEXT)
    cleanup = loaded.build_cleanup_prompt("好。", 1)
    assert cleanup["9"]["inputs"]["value"] == "好。"
    assert cleanup["2"]["inputs"]["text"] == ["9", 0]
    assert cleanup["2"]["inputs"]["seed"] == 1
    assert cleanup["2"]["inputs"]["emotion_description"] == ""


@pytest.mark.parametrize("mutation", ["non_primitive_source", "indirect_source", "other_text_input",
                                       "two_cached_synthesis_nodes"])
def test_idle_workflow_cleanup_rejects_ambiguous_text_routing(tmp_path, graph, mutation):
    upstream_text_graph(graph)
    if mutation == "non_primitive_source":
        graph["9"]["class_type"] = "StringConcatenate"
    elif mutation == "indirect_source":
        graph["proxy"] = {"class_type": "StringProxy", "inputs": {"string": ["9", 0]}}
        graph["2"]["inputs"]["text"] = ["proxy", 0]
    elif mutation == "other_text_input":
        graph["2"]["class_type"] = "IndexTTS25EmotionTextNode"
        graph["2"]["inputs"].update(text="literal template text", emotion_description=["9", 0])
    else:
        graph["2b"] = deepcopy(graph["2"])
        graph["concat"] = {"class_type": "AudioConcat", "inputs": {"a": ["2", 0], "b": ["2b", 0]}}
        graph["4"]["inputs"]["audio"] = ["concat", 0]
    with pytest.raises(WorkflowError, match="cache_cleanup_unsupported"):
        load(tmp_path, graph, **IDLE_WORKFLOW, text_binding=UPSTREAM_TEXT)


def test_idle_workflow_seed_is_validated_on_synthesis_not_text_source(tmp_path, graph):
    upstream_text_graph(graph)
    graph["seed_source"] = {"class_type": "PrimitiveInt", "inputs": {"value": 5}}
    graph["2"]["inputs"]["seed"] = ["seed_source", 0]
    with pytest.raises(WorkflowError, match="cache_cleanup_seed_binding"):
        load(tmp_path, graph, **IDLE_WORKFLOW, text_binding=UPSTREAM_TEXT)


@pytest.mark.parametrize("mutation", ["missing_node", "self_cycle", "long_cycle", "disconnected_cycle", "malformed_link",
                                       "negative_slot", "boolean_slot", "integer_origin", "unknown_field", "not_json"])
def test_invalid_graph_import_rejected(tmp_path, graph, mutation):
    if mutation == "missing_node":
        graph["2"]["inputs"]["reference_audio"] = ["absent", 0]
    elif mutation == "self_cycle":
        graph["2"]["inputs"]["reference_audio"] = ["2", 0]
    elif mutation == "long_cycle":
        graph["1"]["inputs"]["audio"] = ["4", 0]
    elif mutation == "disconnected_cycle":
        graph["extra"] = {"class_type": "Extra", "inputs": {"x": ["extra", 0]}}
    elif mutation == "malformed_link":
        graph["2"]["inputs"]["reference_audio"] = ["1", 0, 1]
    elif mutation == "negative_slot":
        graph["2"]["inputs"]["reference_audio"] = ["1", -1]
    elif mutation == "boolean_slot":
        graph["2"]["inputs"]["reference_audio"] = ["1", False]
    elif mutation == "integer_origin":
        graph["2"]["inputs"]["reference_audio"] = [1, 0]
    elif mutation == "unknown_field":
        graph["2"]["unsafe"] = True
    else:
        graph["2"]["inputs"]["text"] = (1, 2)
    with pytest.raises(WorkflowError):
        import_template(tmp_path, graph, "m", "v", str(uuid4()))
    assert not (tmp_path / "workflows").exists()


def test_long_graph_without_python_recursion(tmp_path):
    graph = {str(i): {"class_type": "N", "inputs": {"x": [str(i - 1), 0]} if i else {}} for i in range(1500)}
    relative, _ = import_template(tmp_path, graph, "m", "v", str(uuid4()))
    assert (tmp_path / relative).is_file()


def test_live_valid_and_pruned_unknown_ignored(tmp_path, graph, schemas):
    graph["unused"] = {"class_type": "Unsupported", "inputs": {}}
    loaded = load(tmp_path, graph)
    original_graph, original_schemas = deepcopy(loaded.graph), deepcopy(schemas)
    assert validate_live(loaded, schemas) == []
    assert loaded.graph == original_graph and schemas == original_schemas


@pytest.mark.parametrize("mutation,code", [
    ("missing_class", "schema_missing_class"), ("missing_required", "schema_missing_required_input"),
    ("bad_output_slot", "schema_output_slot"), ("bad_output_type", "schema_link_type"),
    ("bad_combo", "schema_combo_value"), ("not_output", "schema_audio_output"),
    ("bad_audio_input", "schema_audio_output"), ("bad_type", "schema_input_type"),
    ("bad_range", "schema_input_range"), ("unknown_input", "schema_unknown_input"),
])
def test_live_failure_codes_sanitized(tmp_path, graph, schemas, mutation, code):
    if mutation == "missing_class":
        del schemas["IndexTTS25BaseNode"]
    elif mutation == "missing_required":
        schemas["IndexTTS25BaseNode"]["input"]["required"]["new_input"] = ["STRING"]
    elif mutation == "bad_output_slot":
        graph["4"]["inputs"]["audio"][1] = 99
    elif mutation == "bad_output_type":
        graph["4"]["inputs"]["audio"][1] = 1
    elif mutation == "bad_combo":
        schemas["LoadAudio"]["input"]["required"]["audio"] = [["another-file.wav"]]
    elif mutation == "not_output":
        schemas["SaveAudio"]["output_node"] = False
    elif mutation == "bad_audio_input":
        schemas["SaveAudio"]["input"]["required"]["audio"] = ["IMAGE"]
    elif mutation == "bad_type":
        graph["2"]["inputs"]["seed"] = "not-an-integer"
    elif mutation == "bad_range":
        graph["2"]["inputs"]["duration_factor"] = 999
    else:
        graph["2"]["inputs"]["unknown"] = "secret text"
    errors = validate_live(load(tmp_path, graph), schemas)
    assert code in errors
    assert all(error.startswith("schema_") and "secret" not in error and "一想到" not in error for error in errors)


def test_real_shaped_load_audio_combo_schema(tmp_path, graph, schemas):
    schemas["LoadAudio"]["input"]["required"]["audio"] = ["COMBO", {
        "multiselect": False,
        "options": ["IndexTTS25_reference.wav", "fJ.wav", "example.wav", "example-2.wav"],
        "audio_upload": True,
    }]
    original = deepcopy(schemas)
    assert validate_live(load(tmp_path, graph), schemas) == []
    assert convert_ui_workflow(ui_document(graph), schemas) == graph
    assert schemas == original


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("value,choices,valid", [
    ("ZH", ["ZH", "EN"], True), ("zh", ["ZH"], False),
    (1, [True], False), (True, [1], False), (1.0, [1], False),
    (1, [1], True), (False, [False], True), ("x", [], False),
])
def test_combo_choices_are_type_exact(tmp_path, graph, schemas, modern, value, choices, valid):
    graph["1"]["inputs"]["audio"] = value
    spec = ["COMBO", {"options": choices}] if modern else [choices]
    schemas["LoadAudio"]["input"]["required"]["audio"] = spec
    errors = validate_live(load(tmp_path, graph), schemas)
    assert ("schema_combo_value" not in errors) is valid
    if valid:
        assert errors == []


@pytest.mark.parametrize("spec,code", [
    (["COMBO"], "schema_invalid"), (["COMBO", {}], "schema_invalid"),
    (["COMBO", {"options": "not-a-list"}], "schema_invalid"),
    (["COMBO", {"options": [{"value": "x"}]}], "schema_invalid"),
    (["COMBO", {"options": [float("inf")]}], "schema_invalid"),
    (["COMBO", {"options": ["x"], "multiselect": "false"}], "schema_invalid"),
    (["COMBO", {"options": ["x"], "multiselect": True}], "schema_combo_multiselect_unsupported"),
    ([["x"], {"multiselect": True}], "schema_combo_multiselect_unsupported"),
    (["COMBO", None], "schema_invalid"),
])
def test_combo_malformed_and_multiselect_fail_closed(tmp_path, graph, schemas, spec, code):
    schemas["LoadAudio"]["input"]["required"]["audio"] = spec
    assert code in validate_live(load(tmp_path, graph), schemas)


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize("output_type,valid", [("COMBO", True), ("*", True), ("AUDIO", False), ("STRING", False)])
def test_combo_links_still_require_combo_output(tmp_path, graph, schemas, modern, output_type, valid):
    # Choices named AUDIO/STRING must not be mistaken for class/type alternatives.
    schemas["LoadAudio"]["input"]["required"]["audio"] = (
        ["COMBO", {"options": ["AUDIO", "STRING"]}] if modern else [["AUDIO", "STRING"]]
    )
    graph["options"] = {"class_type": "ChoiceSource", "inputs": {}}
    graph["1"]["inputs"]["audio"] = ["options", 0]
    schemas["ChoiceSource"] = {"input": {"required": {}}, "output": [output_type]}
    errors = validate_live(load(tmp_path, graph), schemas)
    assert ("schema_link_type" not in errors) is valid
    if valid:
        assert errors == []


def test_combo_link_checks_real_output_slot(tmp_path, graph, schemas):
    schemas["LoadAudio"]["input"]["required"]["audio"] = ["COMBO", {"options": ["x"]}]
    graph["options"] = {"class_type": "ChoiceSource", "inputs": {}}
    graph["1"]["inputs"]["audio"] = ["options", 1]
    schemas["ChoiceSource"] = {"input": {"required": {}}, "output": ["COMBO"]}
    assert "schema_output_slot" in validate_live(load(tmp_path, graph), schemas)


@pytest.mark.parametrize("named", [True, False])
def test_ui_exact_maps_and_seed_fixed(graph, schemas, named):
    document = ui_document(graph)
    original = deepcopy(document)
    if not named:
        for node in document["nodes"]:
            del node["widgets_values_named"]
    else:
        # Named widgets must win, regardless of positional stale values/order.
        document["nodes"][1]["widgets_values"] = ["INVALID POSITIONAL CONTENT"]
    before = deepcopy(document)
    converted = convert_ui_workflow(document, schemas)
    assert converted == graph
    assert converted["2"]["inputs"]["seed"] == 484884501
    assert "control_after_generate" not in converted["2"]["inputs"]
    assert "upload" not in converted["1"]["inputs"]
    assert document == before
    assert original["nodes"][1]["widgets_values"][-1] == "randomize"


@pytest.mark.parametrize("mutation", ["unknown", "bypass", "muted", "virtual", "subgraph", "groupnode", "extra_widget",
                                       "missing_widget", "short_positional", "wrong_widget_type", "dangling_link",
                                       "output_slot", "wrong_link_type", "target_slot", "duplicate_link", "duplicate_node"])
def test_ui_rejects_ambiguous_or_unsupported(graph, mutation):
    document = ui_document(graph)
    node = document["nodes"][1]
    if mutation == "unknown":
        node["type"] = "Reroute"
    elif mutation == "bypass":
        node["mode"] = 4
    elif mutation == "muted":
        node["mode"] = 2
    elif mutation == "virtual":
        node["isVirtualNode"] = True
    elif mutation == "subgraph":
        document["definitions"] = {"subgraphs": [{"id": "x"}]}
    elif mutation == "groupnode":
        document["extra"]["groupNodes"] = {"custom": {}}
    elif mutation == "extra_widget":
        node["widgets_values_named"]["future_widget"] = True
    elif mutation == "missing_widget":
        del node["widgets_values_named"]["seed"]
    elif mutation == "short_positional":
        del node["widgets_values_named"]
        node["widgets_values"].pop()
    elif mutation == "wrong_widget_type":
        node["widgets_values_named"]["seed"] = False
    elif mutation == "dangling_link":
        document["links"].pop()
    elif mutation == "output_slot":
        document["links"][0][2] = 99
    elif mutation == "wrong_link_type":
        document["links"][0][-1] = "IMAGE"
    elif mutation == "target_slot":
        document["links"][0][4] = 1
    elif mutation == "duplicate_link":
        document["links"].append(document["links"][0])
    else:
        document["nodes"].append(deepcopy(node))
    with pytest.raises(WorkflowError):
        convert_ui_workflow(document)


@pytest.mark.parametrize("mutation", ["class_list", "socket_name_list", "missing_audio_input", "invalid_mode_bool"])
def test_ui_malformed_types_return_sanitized_error(graph, mutation):
    document = ui_document(graph)
    if mutation == "class_list":
        document["nodes"][0]["type"] = []
    elif mutation == "socket_name_list":
        document["nodes"][1]["inputs"][0]["name"] = []
    elif mutation == "missing_audio_input":
        document["links"] = []
    else:
        document["nodes"][1]["mode"] = False
    with pytest.raises(WorkflowError):
        convert_ui_workflow(document)


def test_template_duplicate_json_keys_rejected_even_with_valid_digest(tmp_path, graph):
    config = register(tmp_path, graph)
    template = tmp_path / config.presets[0].template
    content = b'{"1":{"class_type":"LoadAudio","inputs":{},"inputs":{}}}'
    template.write_bytes(content)
    config.presets[0].sha256 = hashlib.sha256(content).hexdigest()
    with pytest.raises(ConfigError, match="json_duplicate_key"):
        load_presets(config, tmp_path)


def test_ui_live_actual_class_output_slot_validation(graph, schemas):
    schemas["LoadAudio"]["output"] = []
    with pytest.raises(WorkflowError, match="schema_output_slot"):
        convert_ui_workflow(ui_document(graph), schemas)


def symlink_or_skip(link, target):
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        if target.is_dir() and hasattr(exc, "winerror"):
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            pytest.skip(f"symlinks unavailable: {exc.winerror if hasattr(exc, 'winerror') else exc.errno}")


def test_import_rejects_external_workflows_symlink(tmp_path, graph):
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    symlink_or_skip(data / "workflows", outside)
    with pytest.raises(ConfigError, match="path_outside_data_dir"):
        import_template(data, graph, "m", "v", str(uuid4()))
    assert list(outside.iterdir()) == []


def test_load_rejects_external_directory_junction(tmp_path, graph):
    data, outside = tmp_path / "data", tmp_path / "outside"
    config = register(data, graph)
    outside.mkdir()
    template = data / config.presets[0].template
    (outside / "template.json").write_bytes(template.read_bytes())
    symlink_or_skip(data / "workflows" / "linked", outside)
    config.presets[0].template = "workflows/linked/template.json"
    with pytest.raises(ConfigError, match="path_outside_data_dir"):
        load_presets(config, data)


def test_live_text_must_accept_arbitrary_string(tmp_path, graph, schemas):
    schemas["IndexTTS25BaseNode"]["input"]["required"]["text"] = [[graph["2"]["inputs"]["text"]]]
    assert "schema_text_binding" in validate_live(load(tmp_path, graph), schemas)


def test_live_cache_must_be_boolean_not_wildcard(tmp_path, graph, schemas):
    schemas["IndexTTS25CacheControlNode"]["input"]["required"]["keep_models_cached"] = ["*"]
    assert "schema_cache_binding" in validate_live(load(tmp_path, graph), schemas)


def test_live_selected_audio_output_must_be_audio_not_wildcard(tmp_path, graph, schemas):
    schemas["IndexTTS25BaseNode"]["output"][0] = "*"
    assert "schema_audio_output" in validate_live(load(tmp_path, graph), schemas)


def test_concurrent_import_publishes_one_identical_immutable_file(tmp_path, graph):
    from concurrent.futures import ThreadPoolExecutor
    identifier = str(uuid4())
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: import_template(tmp_path, graph, "m", "v", identifier), range(20)))
    assert len(set(results)) == 1
    assert len(list((tmp_path / "workflows").iterdir())) == 1


def test_load_rejects_template_symlink_escape(tmp_path, graph):
    config = register(tmp_path / "data", graph)
    template = tmp_path / "data" / config.presets[0].template
    outside = tmp_path / "outside.json"
    outside.write_bytes(template.read_bytes())
    template.unlink()
    symlink_or_skip(template, outside)
    with pytest.raises(ConfigError):
        load_presets(config, tmp_path / "data")


def test_load_rejects_template_symlink_outside_workflows(tmp_path, graph):
    config = register(tmp_path, graph)
    template = tmp_path / config.presets[0].template
    sibling = tmp_path / "other.json"
    sibling.write_bytes(template.read_bytes())
    template.unlink()
    symlink_or_skip(template, sibling)
    with pytest.raises(WorkflowError, match="workflow_path_escape"):
        load_presets(config, tmp_path)


# --- Unused template cleanup ---------------------------------------------------

def orphan(tmp_path, graph, text="orphan"):
    changed = deepcopy(graph)
    changed["2"]["inputs"]["text"] = text
    return import_template(tmp_path, changed, "m", "v", str(uuid4()))[0]


def test_unused_templates_lists_only_unreferenced_import_names(tmp_path, graph):
    from comfybridge.workflows import managed_templates, unused_templates
    config = register(tmp_path, graph)
    unused = orphan(tmp_path, graph)
    workflows = tmp_path / "workflows"
    (workflows / "example.json").write_text("{}")
    (workflows / "notes.txt").write_text("x")
    (workflows / ".comfybridge-abc.tmp").write_text("x")
    (workflows / (unused.split("/")[1] + ".0123abcd.tmp")).write_text("x")
    nested = workflows / "nested"
    nested.mkdir()
    (nested / Path(unused).name).write_text("{}")
    (workflows / ("preset-" + str(uuid4()) + "-" + "A" * 64 + ".json")).write_text("{}")
    assert managed_templates(tmp_path) == sorted([config.presets[0].template, unused])
    assert unused_templates(tmp_path, config) == [unused]


def test_unused_templates_without_workflows_directory(tmp_path, graph):
    from comfybridge.workflows import remove_unused_templates, unused_templates
    config = register(tmp_path / "other", graph)
    assert unused_templates(tmp_path, config) == []
    assert remove_unused_templates(tmp_path, ["workflows/x.json"], config) == ([], [])


def test_remove_never_deletes_referenced_or_ineligible_candidates(tmp_path, graph):
    from comfybridge.workflows import remove_unused_templates
    config = register(tmp_path, graph)
    referenced = config.presets[0].template
    unused = orphan(tmp_path, graph)
    (tmp_path / "workflows" / "example.json").write_text("{}")
    (tmp_path / "config.json").write_text("{}")
    missing = "workflows/preset-" + str(uuid4()) + "-" + "0" * 64 + ".json"
    candidates = [referenced, unused, missing, "workflows/example.json", "config.json",
                  "workflows/../config.json", "workflows/nested/" + Path(unused).name,
                  "workflows\\" + Path(unused).name, None]
    assert remove_unused_templates(tmp_path, candidates, config) == ([unused], [])
    assert (tmp_path / referenced).exists() and not (tmp_path / unused).exists()
    assert (tmp_path / "workflows/example.json").exists() and (tmp_path / "config.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive file system")
def test_case_variant_reference_still_protects_template(tmp_path, graph):
    from comfybridge.workflows import remove_unused_templates, unused_templates
    config = register(tmp_path, graph)
    template = config.presets[0].template
    config.presets[0].template = "workflows/" + Path(template).name.upper().replace(".JSON", ".json")
    assert unused_templates(tmp_path, config) == []
    assert remove_unused_templates(tmp_path, [template], config) == ([], [])
    assert (tmp_path / template).exists()


def test_remove_reports_failure_without_raising(tmp_path, graph, monkeypatch):
    from comfybridge.workflows import remove_unused_templates
    config = register(tmp_path, graph)
    first, second = sorted([orphan(tmp_path, graph, "one"), orphan(tmp_path, graph, "two")])
    original = Path.unlink
    def unlink(self, *args, **kwargs):
        if self.name == Path(first).name:
            raise PermissionError("locked")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert remove_unused_templates(tmp_path, [first, second], config) == ([second], [first])
    assert (tmp_path / first).exists()


def test_unused_templates_skip_links(tmp_path, graph):
    from comfybridge.workflows import remove_unused_templates, unused_templates
    config = register(tmp_path, graph)
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    link = tmp_path / "workflows" / ("preset-" + str(uuid4()) + "-" + "1" * 64 + ".json")
    symlink_or_skip(link, outside)
    assert unused_templates(tmp_path, config) == []
    assert remove_unused_templates(tmp_path, ["workflows/" + link.name], config) == ([], [])
    assert outside.exists()
