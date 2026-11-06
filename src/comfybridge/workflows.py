"""Immutable API templates and a deliberately limited, lossless UI converter."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import UUID

from .config import (AppConfig, IdleReleaseConfig, PresetConfig, confined_path, parse_json_strict,
                     validate_identifier, validate_template_path, _staged_file)

MAX_CLEANUP_SEED = 4294967295


class WorkflowError(ValueError):
    """A fixed diagnostic code, never an upstream message or user text."""


# These maps describe the supplied IndexTTS 2.5 flat UI format, NOT object_info order.
_WIDGETS = {
    "LoadAudio": ("audio", "upload"),
    "IndexTTS25BaseNode": (
        "text", "lang", "duration_factor", "do_sample_mode", "temperature", "top_p",
        "top_k", "num_beams", "repetition_penalty", "length_penalty", "max_mel_tokens",
        "max_tokens_per_sentence", "interval_silence_ms", "text_normalization", "seed",
        "control_after_generate",
    ),
    "IndexTTS25CacheControlNode": ("keep_models_cached",),
    "SaveAudio": ("filename_prefix",),
}
_UI_ONLY = {"upload", "control_after_generate"}
_UI_OUTPUTS = {
    "LoadAudio": ("AUDIO",),
    "IndexTTS25BaseNode": ("AUDIO", "INT", "STRING"),
    "IndexTTS25CacheControlNode": ("DICT",),
    "SaveAudio": (),
}
_UI_INPUTS = {
    "LoadAudio": {}, "IndexTTS25BaseNode": {"reference_audio": "AUDIO", "cache_control": "DICT"},
    "IndexTTS25CacheControlNode": {}, "SaveAudio": {"audio": "AUDIO"},
}


def _json_bytes(document: object) -> bytes:
    try:
        # Reject Python-only representations instead of silently coercing keys/tuples.
        pending = [document]
        containers = set()
        while pending:
            item = pending.pop()
            if type(item) is dict:
                if id(item) in containers:
                    # Shared Python objects are safe; json.dumps below detects cycles.
                    continue
                containers.add(id(item))
                if any(type(key) is not str for key in item):
                    raise ValueError
                pending.extend(item.values())
            elif type(item) is list:
                if id(item) in containers:
                    continue
                containers.add(id(item))
                pending.extend(item)
            elif type(item) not in (str, bool, int, float, type(None)):
                raise ValueError
        data = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2,
                           allow_nan=False) + "\n").encode("utf-8")
        parse_json_strict(data)
        return data
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise WorkflowError("workflow_invalid_json") from None


def _is_link(value: Any) -> bool:
    return (type(value) is list and len(value) == 2 and type(value[0]) is str
            and type(value[1]) is int and value[1] >= 0)


def _validate_graph(document: object) -> dict:
    if type(document) is not dict or not document or len(document) > 10000:
        raise WorkflowError("workflow_invalid_graph")
    _json_bytes(document)
    for node_id, node in document.items():
        if (type(node_id) is not str or not node_id or len(node_id) > 128
                or type(node) is not dict or set(node) - {"class_type", "inputs", "_meta"}
                or type(node.get("class_type")) is not str or not node["class_type"]
                or type(node.get("inputs")) is not dict
                or ("_meta" in node and type(node["_meta"]) is not dict)):
            raise WorkflowError("workflow_invalid_node")
        for field, value in node["inputs"].items():
            if type(field) is not str or not field:
                raise WorkflowError("workflow_invalid_input")
            if isinstance(value, list):
                if not _is_link(value):
                    raise WorkflowError("workflow_invalid_link")
                if value[0] not in document:
                    raise WorkflowError("workflow_missing_node")
    # Iterative DFS avoids recursion limits on long (but valid) graph chains.
    done = set()
    for root in document:
        active = set()
        stack = [(root, False)]
        while stack:
            node_id, exiting = stack.pop()
            if exiting:
                active.remove(node_id)
                done.add(node_id)
                continue
            if node_id in done:
                continue
            if node_id in active:
                raise WorkflowError("workflow_cycle")
            active.add(node_id)
            stack.append((node_id, True))
            stack.extend((value[0], False) for value in document[node_id]["inputs"].values()
                         if _is_link(value))
    return document


def _ancestors(graph: dict, node_id: str) -> set[str]:
    if node_id not in graph:
        raise WorkflowError("workflow_missing_node")
    found, pending = set(), [node_id]
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(value[0] for value in graph[current]["inputs"].values() if _is_link(value))
    return found


@dataclass(frozen=True)
class LoadedPreset:
    preset: PresetConfig
    graph: dict
    cache_policy: str
    idle_release: IdleReleaseConfig | None = None

    def build_prompt(self, text: str) -> dict:
        if type(text) is not str:
            raise WorkflowError("workflow_text_type")
        if self.cache_policy == "idle_release" and not isinstance(self.idle_release, IdleReleaseConfig):
            raise WorkflowError("workflow_idle_release_settings")
        selected = _ancestors(self.graph, self.preset.audio_output_node)
        result = {node_id: deepcopy(node) for node_id, node in self.graph.items() if node_id in selected}
        binding = self.preset.text_binding
        result[binding.node_id]["inputs"][binding.field] = text
        if self.cache_policy != "inherit":
            binding = self.preset.cache_binding
            if (self.cache_policy == "idle_release" and self.idle_release.method == "free"
                    and binding is None):
                return result  # No invented cache node/binding for a global /free policy.
            if binding is None or self.cache_policy not in ("keep", "release", "idle_release"):
                raise WorkflowError("workflow_cache_binding")
            result[binding.node_id]["inputs"][binding.field] = self.cache_policy in ("keep", "idle_release")
        return result

    def build_cleanup_prompt(self, text: str, seed: int) -> dict:
        """Build an opt-in cleanup graph only; never execute or advance a seed here.

        The backend owns durable seed allocation. Zero is reserved so a sequence
        cannot silently wrap after 4294967295 and reuse a previous cleanup prompt.
        """
        if (self.cache_policy != "idle_release" or self.idle_release is None
                or self.idle_release.method != "workflow"):
            raise WorkflowError("cache_cleanup_unsupported")
        if type(seed) is not int or not 1 <= seed <= MAX_CLEANUP_SEED:
            raise WorkflowError("cache_cleanup_seed_invalid")
        try:
            text = IdleReleaseConfig(method="workflow", text=text).text
        except ValueError:
            raise WorkflowError("cache_cleanup_text") from None
        _validate_graph(self.graph)
        _validate_bindings(self.preset, self.graph, self.cache_policy, self.idle_release)
        result = self.build_prompt(text)
        binding = self.preset.cache_binding
        result[binding.node_id]["inputs"][binding.field] = False
        result[_cleanup_synthesis_node(self.preset, self.graph)]["inputs"]["seed"] = seed
        return result


# Synthesis classes whose text/seed/cache_control inputs share the IndexTTS 2.5 base
# semantics, so a short cleanup generation only changes text, seed and cache flag.
_CLEANUP_SYNTHESIS_CLASSES = frozenset({
    "IndexTTS25BaseNode", "IndexTTS25EmotionTextNode", "IndexTTS25EmotionAudioNode",
    "IndexTTS25EmotionVectorNode",
})
# Literal string sources whose single STRING output passes the value through verbatim.
_CLEANUP_TEXT_PRIMITIVES = frozenset({"PrimitiveString", "PrimitiveStringMultiline"})


def _cache_synthesis_nodes(preset: PresetConfig, graph: dict) -> list[str]:
    """Synthesis nodes on the selected audio path fed by the cache binding and the text."""
    cache, binding = preset.cache_binding, preset.text_binding
    audio_path = _ancestors(graph, graph[preset.audio_output_node]["inputs"]["audio"][0])
    return sorted(node_id for node_id in audio_path
                  if graph[node_id]["inputs"].get("cache_control") == [cache.node_id, 0]
                  and binding.node_id in _ancestors(graph, node_id))


def _cleanup_synthesis_node(preset: PresetConfig, graph: dict) -> str:
    """The single supported synthesis node that receives the cleanup text and seed.

    The text binding must be its `text` input, or the `value` of a string primitive
    linked directly into that input, so the cleanup text is spoken verbatim.
    """
    if preset.cache_binding is None:
        raise WorkflowError("cache_cleanup_unsupported")
    candidates = _cache_synthesis_nodes(preset, graph)
    if len(candidates) != 1 or graph[candidates[0]]["class_type"] not in _CLEANUP_SYNTHESIS_CLASSES:
        raise WorkflowError("cache_cleanup_unsupported")
    node_id, binding = candidates[0], preset.text_binding
    if binding.node_id == node_id:
        if binding.field != "text":
            raise WorkflowError("cache_cleanup_unsupported")
    elif (graph[node_id]["inputs"].get("text") != [binding.node_id, 0]
            or graph[binding.node_id]["class_type"] not in _CLEANUP_TEXT_PRIMITIVES
            or binding.field != "value"):
        raise WorkflowError("cache_cleanup_unsupported")
    return node_id


def _validate_cleanup_binding(preset: PresetConfig, graph: dict) -> None:
    node = graph[_cleanup_synthesis_node(preset, graph)]
    if "seed" in node["inputs"]:
        seed = node["inputs"]["seed"]
        if type(seed) is not int or not 0 <= seed <= MAX_CLEANUP_SEED:
            raise WorkflowError("cache_cleanup_seed_binding")


def _validate_bindings(preset: PresetConfig, graph: dict, policy: str,
                       idle_release: IdleReleaseConfig | None = None) -> None:
    if policy == "idle_release" and not isinstance(idle_release, IdleReleaseConfig):
        raise WorkflowError("workflow_idle_release_settings")
    if policy != "idle_release" and idle_release is not None:
        raise WorkflowError("workflow_idle_release_settings")
    selected = _ancestors(graph, preset.audio_output_node)
    binding = preset.text_binding
    if (binding.node_id not in selected or binding.node_id == preset.audio_output_node
            or type(graph[binding.node_id]["inputs"].get(binding.field)) is not str):
        raise WorkflowError("workflow_text_binding")
    output = graph[preset.audio_output_node]
    if not _is_link(output["inputs"].get("audio")):
        raise WorkflowError("workflow_audio_binding")
    # Text must participate in the selected audio signal, not an unrelated dependency.
    if binding.node_id not in _ancestors(graph, output["inputs"]["audio"][0]):
        raise WorkflowError("workflow_text_binding")
    cache = preset.cache_binding
    needs_cache = policy in ("keep", "release") or (
        policy == "idle_release" and idle_release.method == "workflow"
    )
    if needs_cache and cache is None:
        raise WorkflowError("workflow_cache_binding")
    if cache is not None:
        if (cache.field != "keep_models_cached" or cache.node_id not in graph
                or graph[cache.node_id]["class_type"] != "IndexTTS25CacheControlNode"
                or type(graph[cache.node_id]["inputs"].get(cache.field)) is not bool):
            raise WorkflowError("workflow_cache_binding")
        # This cache node must actually feed (slot zero) the cache_control input of a
        # synthesis node on the selected audio path that also receives the request
        # text. The text binding may be that synthesis node itself or any upstream
        # node (e.g. a multiline string primitive linked into its text input).
        if not _cache_synthesis_nodes(preset, graph):
            raise WorkflowError("workflow_cache_binding")
    if policy == "idle_release" and idle_release.method == "workflow":
        _validate_cleanup_binding(preset, graph)


def load_presets(config: AppConfig, data_dir: Path) -> dict[tuple[str, str], LoadedPreset]:
    config = AppConfig.model_validate(config.model_dump(mode="python"))
    models = {model.id: model for model in config.models}
    loaded = {}
    for preset in config.presets:
        validate_template_path(preset.template)
        path = confined_path(data_dir, preset.template)
        # A workflows directory itself may not escape the data root via a junction.
        workflow_root = confined_path(data_dir, "workflows").resolve()
        try:
            path.resolve().relative_to(workflow_root)
        except ValueError:
            raise WorkflowError("workflow_path_escape") from None
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != preset.sha256:
            raise WorkflowError("workflow_digest_mismatch")
        graph = _validate_graph(parse_json_strict(content))
        model = models[preset.model]
        policy = model.cache_policy
        idle_release = model.idle_release.model_copy(deep=True) if model.idle_release is not None else None
        _validate_bindings(preset, graph, policy, idle_release)
        loaded[(preset.model, preset.voice)] = LoadedPreset(
            preset.model_copy(deep=True), deepcopy(graph), policy, idle_release
        )
    return loaded


def _compatible(expected: str, actual: str) -> bool:
    return expected == "*" or actual == "*" or bool(set(expected.split(",")) & set(actual.split(",")))


def _schema_errors(graph: dict, schemas: dict) -> list[str]:
    errors: set[str] = set()
    for node in graph.values():
        schema = schemas.get(node["class_type"])
        if type(schema) is not dict:
            errors.add("schema_missing_class")
            continue
        inputs = schema.get("input", {})
        if type(inputs) is not dict:
            errors.add("schema_invalid")
            continue
        required, optional = inputs.get("required", {}), inputs.get("optional", {})
        if type(required) is not dict or type(optional) is not dict:
            errors.add("schema_invalid")
            continue
        known = {**required, **optional}
        for field in required:
            if field not in node["inputs"]:
                errors.add("schema_missing_required_input")
        for field, value in node["inputs"].items():
            spec = known.get(field)
            if spec is None:
                errors.add("schema_unknown_input")
                continue
            if not isinstance(spec, (list, tuple)) or not spec:
                errors.add("schema_invalid")
                continue
            expected = spec[0]
            options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            choices = None
            if isinstance(expected, list) or expected == "COMBO":
                # ComfyUI publishes both legacy [[choices], metadata] and newer
                # ["COMBO", {"options": choices, ...}] input schemas. Neither is
                # a list of allowable connection types.
                if len(spec) > 1 and not isinstance(spec[1], dict):
                    errors.add("schema_invalid")
                    continue
                multiselect = options.get("multiselect", False)
                if type(multiselect) is not bool:
                    errors.add("schema_invalid")
                    continue
                if multiselect:
                    # List-valued widgets collide with API link representation;
                    # this MVP deliberately supports single-choice widgets only.
                    errors.add("schema_combo_multiselect_unsupported")
                    continue
                choices = expected if isinstance(expected, list) else options.get("options")
                if (not isinstance(choices, list)
                        or any(type(option) not in (str, bool, int, float, type(None))
                               or (type(option) is float and not math.isfinite(option))
                               for option in choices)):
                    errors.add("schema_invalid")
                    continue
            if _is_link(value):
                source = graph.get(value[0])
                if source is None:
                    errors.add("schema_missing_node")
                    continue
                source_schema = schemas.get(source["class_type"])
                if type(source_schema) is not dict:
                    errors.add("schema_missing_class")
                    continue
                outputs = source_schema.get("output")
                if not isinstance(outputs, (list, tuple)) or value[1] >= len(outputs):
                    errors.add("schema_output_slot")
                elif isinstance(expected, str) and (not isinstance(outputs[value[1]], str)
                                                     or not _compatible(expected, outputs[value[1]])):
                    errors.add("schema_link_type")
                elif isinstance(expected, list) and outputs[value[1]] not in ("COMBO", "*"):
                    errors.add("schema_link_type")
                continue
            if choices is not None:
                if not any(type(value) is type(option) and value == option for option in choices):
                    errors.add("schema_combo_value")
            elif expected == "STRING":
                if type(value) is not str:
                    errors.add("schema_input_type")
            elif expected == "BOOLEAN":
                if type(value) is not bool:
                    errors.add("schema_input_type")
            elif expected in ("INT", "FLOAT"):
                valid = type(value) is int if expected == "INT" else type(value) in (int, float)
                if not valid or (type(value) is float and not math.isfinite(value)):
                    errors.add("schema_input_type")
                elif ((type(options.get("min")) in (int, float) and value < options["min"])
                      or (type(options.get("max")) in (int, float) and value > options["max"])):
                    errors.add("schema_input_range")
            elif expected != "*":
                errors.add("schema_input_requires_link")
    return sorted(errors)


def _cleanup_seed_schema_errors(loaded: LoadedPreset, schemas: dict) -> set[str]:
    """Prove a literal INT seed can span the complete durable cleanup sequence."""
    try:
        node = loaded.graph[_cleanup_synthesis_node(loaded.preset, loaded.graph)]
    except (WorkflowError, KeyError, TypeError, IndexError):
        return {"cache_cleanup_unsupported"}
    schema = schemas.get(node["class_type"])
    inputs = schema.get("input") if isinstance(schema, dict) else None
    if not isinstance(inputs, dict):
        return {"cache_cleanup_seed_schema"}
    specs = [inputs[group]["seed"] for group in ("required", "optional")
             if isinstance(inputs.get(group), dict) and "seed" in inputs[group]]
    if len(specs) != 1:
        return {"cache_cleanup_seed_schema"}
    spec = specs[0]
    if (not isinstance(spec, (list, tuple)) or not spec or spec[0] != "INT"
            or (len(spec) > 1 and not isinstance(spec[1], dict))):
        return {"cache_cleanup_seed_schema"}
    options = spec[1] if len(spec) > 1 else {}
    for name in ("min", "max"):
        if name in options and (type(options[name]) not in (int, float)
                or (type(options[name]) is float and not math.isfinite(options[name]))):
            return {"cache_cleanup_seed_schema"}
    # Missing constraints mean unbounded INT in native Comfy validation.
    if options.get("min", 0) > 0 or options.get("max", MAX_CLEANUP_SEED) < MAX_CLEANUP_SEED:
        return {"cache_cleanup_seed_range"}
    return set()


def validate_live(loaded: LoadedPreset, schemas: dict[str, dict]) -> list[str]:
    """Pure object_info compatibility check; diagnostics never contain template text."""
    try:
        _validate_graph(loaded.graph)
        _validate_bindings(loaded.preset, loaded.graph, loaded.cache_policy, loaded.idle_release)
        graph = loaded.build_prompt(loaded.graph[loaded.preset.text_binding.node_id]["inputs"][loaded.preset.text_binding.field])
    except WorkflowError as error:
        if str(error) in {"cache_cleanup_unsupported", "cache_cleanup_seed_binding"}:
            return [str(error)]
        return ["schema_invalid_graph"]
    except (ValueError, KeyError, TypeError):
        return ["schema_invalid_graph"]
    if type(schemas) is not dict:
        return ["schema_invalid"]
    errors = set(_schema_errors(graph, schemas))

    def input_type(binding):
        binding_schema = schemas.get(graph[binding.node_id]["class_type"], {})
        if not isinstance(binding_schema, dict) or not isinstance(binding_schema.get("input"), dict):
            return None
        for group in ("required", "optional"):
            fields = binding_schema["input"].get(group, {})
            if isinstance(fields, dict) and binding.field in fields:
                spec = fields[binding.field]
                return spec[0] if isinstance(spec, (list, tuple)) and spec else None
        return None

    # A literal matching a combo today is not a valid arbitrary speech-text binding.
    if input_type(loaded.preset.text_binding) != "STRING":
        errors.add("schema_text_binding")
    if loaded.preset.cache_binding and input_type(loaded.preset.cache_binding) != "BOOLEAN":
        errors.add("schema_cache_binding")
    node = graph[loaded.preset.audio_output_node]
    schema = schemas.get(node["class_type"], {})
    if type(schema) is not dict:
        errors.add("schema_audio_output")
    else:
        if schema.get("output_node") is not True:
            errors.add("schema_audio_output")
        inputs = schema.get("input", {})
        audio_spec = None
        if isinstance(inputs, dict):
            for group in ("required", "optional"):
                if isinstance(inputs.get(group), dict) and "audio" in inputs[group]:
                    audio_spec = inputs[group]["audio"]
        if not isinstance(audio_spec, (list, tuple)) or not audio_spec or audio_spec[0] != "AUDIO":
            errors.add("schema_audio_output")
        source_id, slot = node["inputs"]["audio"]
        source_schema = schemas.get(graph[source_id]["class_type"], {})
        outputs = source_schema.get("output", []) if isinstance(source_schema, dict) else []
        if not isinstance(outputs, (list, tuple)) or slot >= len(outputs) or outputs[slot] != "AUDIO":
            errors.add("schema_audio_output")
    if loaded.cache_policy == "idle_release" and loaded.idle_release.method == "workflow":
        errors.update(_cleanup_seed_schema_errors(loaded, schemas))
        try:
            cleanup_graph = loaded.build_cleanup_prompt(loaded.idle_release.text, 1)
        except WorkflowError as error:
            errors.add(str(error) if str(error) in {
                "cache_cleanup_unsupported", "cache_cleanup_seed_binding", "cache_cleanup_text"
            } else "schema_invalid_graph")
        else:
            errors.update(_schema_errors(cleanup_graph, schemas))
    return sorted(errors)


def _ui_id(value: Any) -> str:
    if (type(value) not in (str, int) or (type(value) is int and value < 0)
            or (type(value) is str and (not value or len(value) > 128))):
        raise WorkflowError("ui_invalid_id")
    return str(value)


def convert_ui_workflow(document: dict, schemas: dict | None = None) -> dict:
    """Convert only the known flat active IndexTTS 2.5 graph; never guess widgets."""
    if type(document) is not dict or type(document.get("nodes")) is not list or type(document.get("links")) is not list:
        raise WorkflowError("ui_invalid_graph")
    if document.get("definitions") or document.get("subgraphs"):
        raise WorkflowError("ui_subgraph_unsupported")
    extra = document.get("extra", {})
    if not isinstance(extra, dict) or extra.get("groupNodes"):
        raise WorkflowError("ui_subgraph_unsupported")
    graph, ui_nodes = {}, {}
    for node in document["nodes"]:
        if type(node) is not dict:
            raise WorkflowError("ui_invalid_node")
        node_id = _ui_id(node.get("id"))
        kind = node.get("type")
        if type(kind) is not str or kind not in _WIDGETS:
            raise WorkflowError("ui_unsupported_class")
        if type(node.get("mode", 0)) is not int or node.get("mode", 0) != 0:
            raise WorkflowError("ui_inactive_node")
        if node_id in graph:
            raise WorkflowError("ui_duplicate_node")
        if node.get("subgraph") or node.get("isVirtualNode") or node.get("is_virtual"):
            raise WorkflowError("ui_subgraph_unsupported")
        names = _WIDGETS[kind]
        if "widgets_values_named" in node:
            values = node["widgets_values_named"]
            if (type(values) is not dict or set(values) - set(names)
                    or set(names) - _UI_ONLY - set(values)):
                raise WorkflowError("ui_widget_map")
            values = deepcopy(values)
        else:
            positional = node.get("widgets_values")
            if type(positional) is not list or len(positional) != len(names):
                raise WorkflowError("ui_widget_map")
            values = dict(zip(names, deepcopy(positional), strict=True))
        for key in _UI_ONLY:
            values.pop(key, None)
        for key, value in values.items():
            if key in {"text", "lang", "do_sample_mode", "audio", "filename_prefix"}:
                valid = type(value) is str
            elif key in {"text_normalization", "keep_models_cached"}:
                valid = type(value) is bool
            elif key in {"top_k", "num_beams", "max_mel_tokens", "max_tokens_per_sentence", "interval_silence_ms", "seed"}:
                valid = type(value) is int
            else:
                valid = type(value) in (int, float) and math.isfinite(value)
            if not valid:
                raise WorkflowError("ui_widget_type")
        graph[node_id] = {"class_type": kind, "inputs": values}
        ui_nodes[node_id] = node
    links = {}
    for link in document["links"]:
        if type(link) is not list or len(link) != 6:
            raise WorkflowError("ui_invalid_link")
        link_id, origin, origin_slot, target, target_slot, declared_type = link
        link_id, origin, target = _ui_id(link_id), _ui_id(origin), _ui_id(target)
        if (link_id in links or origin not in graph or target not in graph
                or type(origin_slot) is not int or origin_slot < 0
                or type(target_slot) is not int or target_slot < 0):
            raise WorkflowError("ui_invalid_link")
        outputs = _UI_OUTPUTS[graph[origin]["class_type"]]
        if origin_slot >= len(outputs) or declared_type != outputs[origin_slot]:
            raise WorkflowError("ui_output_slot")
        target_inputs = ui_nodes[target].get("inputs", [])
        if type(target_inputs) is not list or target_slot >= len(target_inputs):
            raise WorkflowError("ui_target_slot")
        socket = target_inputs[target_slot]
        if (type(socket) is not dict or type(socket.get("name")) is not str
                or _ui_id(socket.get("link")) != link_id
                or socket.get("type") != declared_type
                or _UI_INPUTS[graph[target]["class_type"]].get(socket.get("name")) != declared_type
                or socket["name"] in graph[target]["inputs"]):
            raise WorkflowError("ui_target_slot")
        graph[target]["inputs"][socket["name"]] = [origin, origin_slot]
        links[link_id] = (target, target_slot)
    for node_id, node in ui_nodes.items():
        inputs = node.get("inputs", [])
        if type(inputs) is not list:
            raise WorkflowError("ui_invalid_input")
        seen = set()
        for slot, socket in enumerate(inputs):
            if type(socket) is not dict or type(socket.get("name")) is not str or socket.get("name") in seen:
                raise WorkflowError("ui_invalid_input")
            seen.add(socket.get("name"))
            if socket.get("name") not in _UI_INPUTS[node["type"]]:
                raise WorkflowError("ui_invalid_input")
            if socket.get("link") is not None and links.get(_ui_id(socket["link"])) != (node_id, slot):
                raise WorkflowError("ui_missing_link")
        if node["type"] == "IndexTTS25BaseNode" and "reference_audio" not in graph[node_id]["inputs"]:
            raise WorkflowError("ui_missing_link")
        if node["type"] == "SaveAudio" and "audio" not in graph[node_id]["inputs"]:
            raise WorkflowError("ui_missing_link")
    _validate_graph(graph)
    if schemas is not None:
        if type(schemas) is not dict:
            raise WorkflowError("schema_invalid")
        errors = _schema_errors(graph, schemas)
        if errors:
            raise WorkflowError(errors[0])
    return graph


def import_template(data_dir: Path, document: dict, model: str, voice: str, preset_id: str) -> tuple[str, str]:
    validate_identifier(model)
    validate_identifier(voice)
    try:
        if str(UUID(preset_id)) != preset_id:
            raise ValueError
    except (ValueError, AttributeError):
        raise WorkflowError("invalid_preset_id") from None
    _validate_graph(document)
    content = _json_bytes(document)
    digest = hashlib.sha256(content).hexdigest()
    # Prefix is cosmetic only; UUID + full digest provide unambiguous identity.
    relative = f"workflows/preset-{preset_id}-{digest}.json"
    root = Path(data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    directory = confined_path(root, "workflows")
    directory.mkdir(parents=True, exist_ok=True)
    path = confined_path(root, relative)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_bytes() != content:
            raise WorkflowError("workflow_immutable_conflict")
        return relative, digest
    staged = _staged_file(directory, content)
    try:
        # Windows rename is atomic and never replaces an existing destination (also
        # works on FAT). POSIX rename overwrites, so use no-clobber hard-link publish.
        try:
            if os.name == "nt":
                os.rename(staged, path)
            else:
                os.link(staged, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != content:
                raise WorkflowError("workflow_immutable_conflict") from None
    finally:
        staged.unlink(missing_ok=True)
    return relative, digest


# Only names produced by import_template are ever eligible for removal.
_PRESET_TEMPLATE = re.compile(
    r"preset-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-[0-9a-f]{64}\.json\Z")


def referenced_templates(config: AppConfig) -> set[str]:
    """Template paths of the given configuration, case-normalized for this OS."""
    return {os.path.normcase(preset.template) for preset in config.presets}


def _is_fs_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def managed_templates(data_dir: Path) -> list[str]:
    """Regular, non-link preset template files directly inside ``workflows``.

    Hand-placed JSON, temporary files, subdirectories and links are never listed.
    """
    root = Path(data_dir).resolve()
    directory = confined_path(root, "workflows")
    if _is_fs_link(directory) or not directory.is_dir():
        return []
    result = []
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if (_PRESET_TEMPLATE.fullmatch(entry.name) and not _is_fs_link(path)
                    and entry.is_file(follow_symlinks=False)):
                result.append("workflows/" + entry.name)
    return sorted(result)


def unused_templates(data_dir: Path, config: AppConfig) -> list[str]:
    """Managed preset templates not referenced by ``config``."""
    referenced = referenced_templates(config)
    return [item for item in managed_templates(data_dir) if os.path.normcase(item) not in referenced]


def remove_unused_templates(data_dir: Path, candidates: Iterable[str],
                            config: AppConfig) -> tuple[list[str], list[str]]:
    """Permanently delete candidates that ``config`` does not reference.

    Callers should hold the configuration lock. Ineligible candidates (referenced,
    not an import_template name, outside ``workflows`` or a link) are skipped
    silently. Returns ``(removed, failed)``; a missing file is neither.
    """
    root = Path(data_dir).resolve()
    referenced = referenced_templates(config)
    removed: list[str] = []
    failed: list[str] = []
    try:
        directory = confined_path(root, "workflows")
        if _is_fs_link(directory) or not directory.is_dir():
            return removed, failed
        workflow_root = directory.resolve()
    except (ValueError, OSError):
        return removed, failed
    for relative in sorted({item for item in candidates if type(item) is str}):
        if os.path.normcase(relative) in referenced:
            continue
        parts = relative.split("/")
        if len(parts) != 2 or parts[0] != "workflows" or not _PRESET_TEMPLATE.fullmatch(parts[1]):
            continue
        try:
            validate_template_path(relative)
            path = confined_path(root, relative)
            if path.resolve().parent != workflow_root or _is_fs_link(path):
                continue
            if not path.exists():
                continue
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed.append(relative)
        except OSError:
            failed.append(relative)
        except ValueError:
            continue
    return removed, failed
