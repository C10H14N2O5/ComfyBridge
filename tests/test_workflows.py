from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from comfybridge.config import AppConfig, ConfigError, parse_json_strict
from comfybridge.workflows import (
    WorkflowError, convert_ui_workflow, import_template, load_presets, validate_live,
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


def register(tmp_path, graph, *, policy="inherit", cache=True, **preset_overrides):
    identifier = str(uuid4())
    relative, digest = import_template(tmp_path, graph, "index-tts-2.5", "example", identifier)
    preset = dict(id=identifier, model="index-tts-2.5", voice="example", template=relative,
                  sha256=digest, text_binding={"node_id": "2", "field": "text"}, audio_output_node="4")
    if cache:
        preset["cache_binding"] = {"node_id": "3", "field": "keep_models_cached"}
    preset.update(preset_overrides)
    config = AppConfig.model_validate({"server": {"api_key": "test-only-key"}, "comfyui": {},
                                      "models": [{"id": "index-tts-2.5", "cache_policy": policy}], "presets": [preset]})
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
