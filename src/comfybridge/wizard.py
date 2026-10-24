"""Local, offline-only menu editor. Never sends a generation request."""
from __future__ import annotations

import copy
import getpass
from pathlib import Path
import uuid

from .platform_windows import InstanceLock
from .i18n import UIContext, safe_text


MESSAGES = {
    'invalid_fields': ('请修正以下配置字段：{details}', 'Correct these configuration fields: {details}'),
    'workflow_error': ('配置或工作流校验失败：{code}。请检查绑定和模板，或导出 API 格式 JSON。', 'Configuration/workflow validation: {code}. Check bindings/template or export API JSON.'),
    'missing_file': ('所需配置或模板文件不存在。请先配置，或选择已有 JSON 文件。', 'Required configuration/template file missing. Configure first or select an existing JSON file.'),
    'permission_error': ('数据目录不可写或无法保护。请指定当前用户可写的 --data-dir；不会自动换目录。', 'Data directory is not writable/private. Choose a current-user writable --data-dir; no automatic fallback.'),
    'io_error': ('本地读写失败。请检查目录权限、文件和监听端口。', 'Local I/O failed. Check directory access, files and the listening port.'),
    'invalid_operation': ('选择、数值或操作无效。请检查编号、必填密钥、绑定和当前状态。', 'Invalid selection/value or local operation rejected. Check menu numbers, required key, bindings and instance state.'),
    'invalid_selection': ('输入无效，请选择显示的编号。', 'Invalid input; select a displayed option.'),
    'no_candidates': ('暂无可选项目，请先添加模型或导入有效工作流。', 'No candidates. Add a model or import a valid workflow first.'),
    'duplicate_model': ('模型 ID 已存在，请使用修改功能。', 'Model ID already exists; choose edit instead.'),
    'unknown_model': ('模型不存在，请先添加模型。', 'Model does not exist; add a model first.'),
    'model_has_presets': ('模型仍有预设；如需删除，请明确确认同时删除其预设。', 'Model has presets; explicitly confirm deleting them with the model.'),
    'duplicate_preset_choose_edit': ('该模型和音色组合已存在，请使用修改功能。', 'This model/voice pair already exists; choose edit instead.'),
    'unknown_preset': ('预设不存在，请重新选择。', 'Preset does not exist; select again.'),
    'template_too_large': ('工作流 JSON 超过 16 MiB 限制。', 'Workflow JSON exceeds the 16 MiB limit.'),
    'invalid_workflow': ('工作流格式无效，请选择已知界面工作流或 API 格式 JSON。', 'Invalid workflow; choose a supported UI workflow or API-format JSON.'),
    'configuration': ('配置', 'Configuration'),
    'summary': ('配置摘要', 'Configuration summary'),
    'server_title': ('服务与密钥', 'Server and key'),
    'models_title': ('模型与缓存策略', 'Models and cache policy'),
    'presets_title': ('音色预设', 'Voice presets'),
    'limits_title': ('限额与日志', 'Limits and logging'),
    'editor_title': ('配置编辑器', 'Configuration editor'),
    'first_title': ('首次配置向导', 'First-time setup'),
    'list': ('查看列表', 'View list'),
    'edit_server': ('修改监听、业务密钥和 ComfyUI 地址', 'Edit listener, business key and ComfyUI URL'),
    'add_model': ('新增模型', 'Add model'),
    'edit_model': ('修改模型或缓存策略', 'Edit model or cache policy'),
    'delete_model': ('删除模型', 'Delete model'),
    'add_preset': ('新增音色预设', 'Add voice preset'),
    'edit_preset': ('修改预设或替换模板', 'Edit preset or replace template'),
    'delete_preset': ('删除预设', 'Delete preset'),
    'edit_limits': ('修改限额或日志设置', 'Edit limits or logging'),
    'save_menu': ('校验并保存', 'Validate and save'),
    'return_menu': ('返回（Q 也可返回）', 'Return (Q also returns)'),
    'action': ('请选择：', 'Select: '),
    'select_number': ('请选择编号（不会自动猜选）：', 'Select number (no automatic default): '),
    'confirm_yes': ('确认', 'Confirm'),
    'confirm_no': ('取消（默认）', 'Cancel (default)'),
    'value': ('{label} [{current}]：', '{label} [{current}]: '),
    'business_key': ('业务 API Key（留空保留已有密钥；首次必填）：', 'Business API key (blank keeps existing; required on first run): '),
    'listen_host': ('监听地址', 'Listen host'),
    'listen_port': ('监听端口', 'Listen port'),
    'comfy_url': ('ComfyUI 地址', 'ComfyUI URL'),
    'security_note': ('业务密钥仅授权语音调用；管理通道仍为私有回环地址。不会修改防火墙。', 'Business key grants TTS access only. Admin remains private loopback. Firewall unchanged.'),
    'settings': ('设置类别', 'Settings category'),
    'limits': ('请求、队列、时限与音频限额', 'Request, queue, timeout and audio limits'),
    'logging': ('日志轮转设置', 'Rotating log settings'),
    'setting': ('选择设置项', 'Select setting'),
    'model': ('选择模型', 'Select model'),
    'model_id': ('模型 ID', 'Model ID'),
    'voice_id': ('音色 ID', 'Voice ID'),
    'preset': ('选择预设', 'Select preset'),
    'cache_strategy': ('缓存策略（重启后应用；现在不会卸载模型）', 'Cache strategy (applies after restart; does not unload now)'),
    'inherit': ('跟随工作流', 'Follow workflow'),
    'keep': ('保持缓存', 'Keep cache'),
    'release': ('生成后不保留', 'Do not retain after generation'),
    'idle_release': ('定时丢弃', 'Release after idle timeout'),
    'idle_seconds': ('空闲等待秒数（1–86400）', 'Idle timeout in seconds (1–86400)'),
    'idle_method': ('明确选择定时丢弃方式', 'Explicitly select the idle-release method'),
    'idle_free': ('ComfyUI /free：请求实例级卸载', 'ComfyUI /free: request instance-wide unloading'),
    'idle_workflow': ('工作流：运行一次短文本清理任务', 'Workflow: run a short-text cleanup task'),
    'idle_free_warning': ('此方式会请求卸载整个 ComfyUI 实例的模型，可能影响其他工作流和用户；只在远端队列为空时请求，但无法锁定其他客户端。请求被受理不证明显存已释放。仅保存配置不会立即发送请求。确认启用？', 'This requests model unloading across the entire ComfyUI instance and may affect other workflows/users. It only requests while the remote queue is empty, but cannot lock other clients. Acceptance does not prove VRAM was freed. Saving settings sends no request now. Enable this method?'),
    'idle_workflow_warning': ('此方式在空闲到期后会提交真实 GPU 工作流，并可能在 ComfyUI 保留音频产物；使用该模型最后一次提交的音色预设，同一模型下所有音色必须共享可释放的缓存。清理工作流完成也不证明显存已释放。仅保存配置不会运行生成。确认启用？', 'After the idle timeout this method submits a real GPU workflow and may leave audio artifacts in ComfyUI. It uses the last submitted preset; all voices of this model must share a releasable cache. Workflow completion does not prove VRAM was freed. Saving settings runs no generation. Enable this method?'),
    'idle_text': ('清理任务短文本（1–64 字符；留空保留）', 'Cleanup task text (1–64 characters; blank keeps current)'),
    'idle_summary': ('    空闲 {seconds} 秒后；方式：{method}', '    After {seconds} idle seconds; method: {method}'),
    'idle_release_settings_required': ('定时丢弃必须设置等待秒数和明确选择清理方式。', 'Idle release requires settings and an explicitly selected method.'),
    'idle_release_settings_not_applicable': ('非定时丢弃策略不能保留定时清理设置。', 'Other cache policies cannot retain idle-release settings.'),
    'invalid_idle_release_text': ('清理文本必须为 1–64 字符且不能全为空白；请勿包含控制字符。', 'Cleanup text must contain 1–64 characters and must not be blank or contain control characters.'),
    'workflow_idle_release_settings': ('工作流的定时丢弃设置与缓存策略不一致。', 'Workflow idle-release settings do not match the cache policy.'),
    'cache_cleanup_unsupported': ('工作流清理需要 IndexTTS25BaseNode 的 text 输入；请更换模板或使用 /free。', 'Workflow cleanup requires the IndexTTS25BaseNode text input; change the template or use /free.'),
    'cache_cleanup_seed_binding': ('清理模板的 seed 必须为 32 位非负整数或省略，不能连接其他节点。', 'Cleanup template seed must be a literal unsigned 32-bit integer or omitted, not a node link.'),
    'cache_cleanup_seed_invalid': ('清理任务种子无效或已耗尽；不会提交任务。', 'Cleanup task seed is invalid or exhausted; no task will be submitted.'),
    'cache_cleanup_text': ('清理任务短文本无效，请检查长度和空白字符。', 'Cleanup task text is invalid; check its length and whitespace.'),
    'cache_cleanup_seed_schema': ('远端节点必须提供兼容的 INT seed 输入。', 'The remote node must expose a compatible INT seed input.'),
    'cache_cleanup_seed_range': ('远端 seed 输入必须支持完整的 0–4294967295 范围。', 'The remote seed input must support the complete 0–4294967295 range.'),
    'workflow_file': ('工作流 JSON 文件（修改时留空保留旧模板；0 返回）：', 'Workflow JSON file (blank retains old template when editing; 0 returns): '),
    'import_note': ('只复制 JSON，不复制参考音频或模型文件；不会执行生成。', 'Import copies JSON only, not reference audio/model assets. No generation will run.'),
    'select_text': ('明确选择文本注入字段', 'Explicitly select text injection field'),
    'select_output': ('明确选择最终音频输出节点', 'Explicitly select final audio output node'),
    'select_cache': ('明确选择缓存控制绑定', 'Explicitly select cache binding'),
    'no_cache': ('不绑定缓存控制（仅限跟随工作流或定时 /free）', 'No cache binding (inherit or timed /free only)'),
    'apply_preset': ('将此预设变更应用到草稿？', 'Apply this preset change to the draft?'),
    'confirm_delete_model': ('删除此模型及其全部预设？', 'Delete this model and ALL its presets?'),
    'confirm_delete_preset': ('删除此预设登记（保留不可变 JSON 文件）？', 'Delete this registration (keep immutable JSON files)?'),
    'invalid_existing': ('现有配置无效。将从空草稿修复；明确保存前不会修改原文件，保存时会备份。', 'Existing configuration is invalid. Repair starts with an empty draft; the original stays until explicitly saved with backup.'),
    'not_saved': ('未保存配置。请修正草稿；没有执行生成。', 'No configuration saved. Correct the draft; no generation was run.'),
    'save_note': ('只校验和保存：不执行生成、不热加载、不重启、不修改登录启动。', 'Validate/save only: no generation, no hot reload, no restart, no Startup changes.'),
    'confirm_save': ('保存此已校验配置？', 'Save this validated configuration?'),
    'external_change': ('配置已被外部修改，拒绝覆盖。草稿未写入；请退出后重新打开编辑器。', 'Configuration changed externally; refusing to overwrite. Draft not written; exit and reopen the editor.'),
    'saved_current': ('已保存／当前运行配置一致。', 'Saved / current running configuration matches.'),
    'saved_pending': ('已保存／待启动后台或明确重启后生效。', 'Saved / pending backend start or explicit restart.'),
    'dirty': ('草稿有未保存的更改。', 'The draft has unsaved changes.'),
    'dirty_save': ('校验并保存后返回', 'Validate/save and return'),
    'dirty_discard': ('放弃草稿并返回', 'Discard draft and return'),
    'dirty_continue': ('继续编辑', 'Continue editing'),
    'first_model': ('现在添加第一个模型？也可稍后从主菜单添加。', 'Add the first model now? You may defer it to the home menu.'),
    'first_preset': ('现在导入第一个音色预设？也可稍后从主菜单添加。', 'Import the first voice preset now? You may defer it to the home menu.'),
    'deferred': ('尚未登记可用音色预设，语音服务未就绪；可稍后添加模型和预设。', 'No voice preset is registered; TTS is not ready. Add models and presets later.'),
    'first_cancel_save': ('尚未保存；可继续修正草稿，或返回并明确放弃。', 'Not saved; continue correcting the draft, or return and explicitly discard it.'),
    'empty': ('（暂无）', '(none)'),
    'redacted': ('已隐藏', '[redacted]'),
    'key_label': ('业务密钥', 'Business key'),
    'template': ('模板', 'Template'),
    'text_binding': ('文本绑定', 'Text binding'),
    'output_binding': ('音频输出节点', 'Audio output node'),
    'cache_binding': ('缓存控制绑定', 'Cache binding'),
    'max_input_chars': ('最大输入字符数', 'Maximum input characters'),
    'max_body_bytes': ('最大请求体字节数', 'Maximum request body bytes'),
    'max_inflight': ('最大在途任务数', 'Maximum in-flight tasks'),
    'max_queue': ('最大等待队列长度', 'Maximum waiting queue length'),
    'request_timeout': ('请求总时限（秒）', 'Total request timeout (seconds)'),
    'connect_timeout': ('连接时限（秒）', 'Connect timeout (seconds)'),
    'query_timeout': ('查询时限（秒）', 'Query timeout (seconds)'),
    'submit_timeout': ('提交时限（秒）', 'Submission timeout (seconds)'),
    'download_timeout': ('下载时限（秒）', 'Download timeout (seconds)'),
    'transcode_timeout': ('转码时限（秒）', 'Transcode timeout (seconds)'),
    'poll_interval': ('任务轮询间隔（秒）', 'Task polling interval (seconds)'),
    'probe_interval': ('离线探测间隔（秒）', 'Offline probe interval (seconds)'),
    'max_audio_bytes': ('最大音频字节数', 'Maximum audio bytes'),
    'max_audio_seconds': ('最大音频时长（秒）', 'Maximum audio duration (seconds)'),
    'max_bytes': ('单个日志文件最大字节数', 'Maximum bytes per log file'),
    'backup_count': ('日志备份数量', 'Log backup count'),
}


_DIAGNOSTIC_TYPES = frozenset({'missing', 'extra_forbidden', 'value_error', 'literal_error',
    'string_type', 'string_too_short', 'string_too_long', 'string_pattern_mismatch', 'int_type',
    'int_parsing', 'int_parsing_size', 'int_from_float', 'float_type', 'float_parsing', 'finite_number',
    'greater_than', 'greater_than_equal', 'less_than', 'less_than_equal', 'list_type', 'dict_type',
    'model_type', 'model_attributes_type', 'bool_type', 'none_required'})
_WORKFLOW_DIAGNOSTICS = frozenset({
    'json_duplicate_key', 'json_nonfinite', 'json_invalid', 'path_outside_data_dir', 'config_symlink',
    'workflow_invalid_json', 'workflow_invalid_graph', 'workflow_invalid_node', 'workflow_invalid_input',
    'workflow_invalid_link', 'workflow_missing_node', 'workflow_cycle', 'workflow_text_type',
    'workflow_cache_binding', 'workflow_text_binding', 'workflow_audio_binding', 'workflow_path_escape',
    'workflow_digest_mismatch', 'workflow_immutable_conflict', 'workflow_idle_release_settings',
    'cache_cleanup_unsupported', 'cache_cleanup_seed_binding', 'cache_cleanup_seed_invalid',
    'cache_cleanup_text', 'cache_cleanup_seed_schema', 'cache_cleanup_seed_range',
    'ui_invalid_id', 'ui_invalid_graph', 'ui_subgraph_unsupported', 'ui_invalid_node',
    'ui_unsupported_class', 'ui_inactive_node', 'ui_duplicate_node', 'ui_widget_map', 'ui_widget_type',
    'ui_invalid_link', 'ui_output_slot', 'ui_target_slot', 'ui_invalid_input', 'ui_missing_link',
    'invalid_preset_id', 'schema_missing_class', 'schema_invalid', 'schema_invalid_graph',
    'schema_missing_required_input', 'schema_unknown_input', 'schema_combo_multiselect_unsupported',
    'schema_missing_node', 'schema_output_slot', 'schema_link_type', 'schema_combo_value',
    'schema_input_type', 'schema_input_range', 'schema_input_requires_link', 'schema_text_binding',
    'schema_cache_binding', 'schema_audio_output'})


def diagnostic(error: Exception, ui=None) -> str:
    """Actionable locations/codes without input/context/raw exception strings."""
    from pydantic import ValidationError
    from .config import ConfigError
    from .workflows import WorkflowError
    ui = ui or UIContext()
    if isinstance(error, ValidationError):
        from .config import (AppConfig, Binding, ComfyConfig, IdleReleaseConfig, LimitsConfig,
                             LoggingConfig, ModelConfig, PresetConfig, ServerConfig)
        # Pydantic locations are not inherently safe: extra_forbidden and mapping
        # validation may include arbitrary input keys, even when input/context are
        # omitted. Display only trusted schema field names and list indices.
        known_fields = {name for model in (AppConfig, Binding, ComfyConfig, IdleReleaseConfig,
                                          LimitsConfig, LoggingConfig, ModelConfig, PresetConfig, ServerConfig)
                        for name in model.model_fields}
        details = []
        for item in error.errors(include_input=False, include_context=False, include_url=False)[:8]:
            parts = item['loc']
            safe_location = item['type'] != 'extra_forbidden' and all(
                (type(part) is str and part in known_fields) or (type(part) is int and part >= 0)
                for part in parts)
            location = '.'.join(str(part) for part in parts) if safe_location else 'configuration'
            kind = item['type'] if item['type'] in _DIAGNOSTIC_TYPES else 'invalid_value'
            details.append(f'{location or "configuration"}: {kind}')
        return ui.t(MESSAGES, 'invalid_fields', details='; '.join(details))
    if isinstance(error, (ConfigError, WorkflowError)):
        code = str(error)
        if code in _WORKFLOW_DIAGNOSTICS:
            return ui.t(MESSAGES, code) if code in MESSAGES else ui.t(MESSAGES, 'workflow_error', code=code)
    if isinstance(error, FileNotFoundError):
        return ui.t(MESSAGES, 'missing_file')
    if isinstance(error, PermissionError):
        return ui.t(MESSAGES, 'permission_error')
    if isinstance(error, OSError):
        return ui.t(MESSAGES, 'io_error')
    codes = {'no_candidates', 'duplicate_model', 'unknown_model', 'model_has_presets',
             'duplicate_preset_choose_edit', 'unknown_preset', 'template_too_large', 'invalid_workflow', 'invalid_selection',
             'idle_release_settings_required', 'idle_release_settings_not_applicable', 'invalid_idle_release_text'}
    if isinstance(error, ValueError) and str(error) in codes:
        return ui.t(MESSAGES, str(error))
    return ui.t(MESSAGES, 'invalid_operation')


def summary(config) -> dict:
    data = config.model_dump(mode='json') if hasattr(config, 'model_dump') else copy.deepcopy(config)
    data.setdefault('server', {})['api_key'] = '[redacted]'
    return data


def validate_draft(data_dir: Path, draft: dict):
    from .config import AppConfig
    from .workflows import load_presets
    config = AppConfig.model_validate(draft)
    load_presets(config, data_dir)
    return config


def put_model(draft: dict, model_id: str, cache_policy: str = 'inherit', *, replace: str | None = None,
              idle_release: dict | None = None) -> None:
    from .config import ModelConfig
    models = draft.get('models', [])
    previous = next((m for m in models if m['id'] == replace), None) if replace is not None else None
    # Preserve an unchanged idle policy when a programmatic edit only renames it.
    if idle_release is None and cache_policy == 'idle_release' and previous:
        idle_release = copy.deepcopy(previous.get('idle_release'))
    payload = {'id': model_id, 'cache_policy': cache_policy}
    if idle_release is not None:
        payload['idle_release'] = idle_release
    # Policy/settings consistency is validated atomically, never by assignments.
    model = ModelConfig.model_validate(payload).model_dump(mode='json', exclude_none=True)
    if any(m['id'] == model_id and m['id'] != replace for m in models):
        raise ValueError('duplicate_model')
    if replace is None:
        draft.setdefault('models', []).append(model)
    else:
        index = next((i for i, m in enumerate(models) if m['id'] == replace), None)
        if index is None:
            raise ValueError('unknown_model')
        models[index] = model
        for preset in draft.get('presets', []):
            if preset['model'] == replace:
                preset['model'] = model_id


def delete_model(draft: dict, model_id: str, *, cascade: bool = False) -> None:
    used = [p for p in draft.get('presets', []) if p['model'] == model_id]
    if used and not cascade:
        raise ValueError('model_has_presets')
    draft['models'] = [m for m in draft.get('models', []) if m['id'] != model_id]
    if cascade:
        draft['presets'] = [p for p in draft.get('presets', []) if p['model'] != model_id]


def read_import(source: Path) -> dict:
    from .config import parse_json_strict
    from .workflows import convert_ui_workflow
    if source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('template_too_large')
    document = parse_json_strict(source.read_bytes())
    if not isinstance(document, dict):
        raise ValueError('invalid_workflow')
    if isinstance(document.get('nodes'), list):
        document = convert_ui_workflow(document)
    if not document or len(document) > 10000:
        raise ValueError('invalid_workflow')
    for node_id, node in document.items():
        if (not isinstance(node_id, str) or not isinstance(node, dict)
                or not isinstance(node.get('class_type'), str)
                or not isinstance(node.get('inputs'), dict)
                or not isinstance(node.get('_meta', {}), dict)):
            raise ValueError('invalid_workflow')
    return document


def node_choices(graph: dict) -> list[tuple[str, str]]:
    choices = []
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            continue
        title = node.get('_meta', {}).get('title', '')
        # Titles are display-only; no text input values or raw templates are printed.
        clean = ''.join(c for c in str(title) if c.isprintable())[:100]
        choices.append((str(node_id), f"{node_id}: {node.get('class_type', '?')} {clean}"))
    return choices


def binding_choices(graph: dict, *, cache: bool = False) -> list[tuple[dict, str]]:
    result = []
    for node_id, label in node_choices(graph):
        node = graph[node_id]
        for field, value in node.get('inputs', {}).items():
            if cache:
                valid = node.get('class_type') == 'IndexTTS25CacheControlNode' and field == 'keep_models_cached' and type(value) is bool
            else:
                valid = isinstance(value, str)
            if valid:
                result.append(({'node_id': node_id, 'field': field}, label + f' / {field}'))
    return result


def put_preset(data_dir: Path, draft: dict, graph: dict, *, model: str, voice: str,
               text_binding: dict, audio_output_node: str, cache_binding: dict | None = None,
               replace_id: str | None = None) -> dict:
    from .config import PresetConfig
    from .workflows import import_template
    if model not in {m['id'] for m in draft.get('models', [])}:
        raise ValueError('unknown_model')
    if any(p['model'] == model and p['voice'] == voice and p['id'] != replace_id for p in draft.get('presets', [])):
        raise ValueError('duplicate_preset_choose_edit')
    if replace_id and not any(p['id'] == replace_id for p in draft.get('presets', [])):
        raise ValueError('unknown_preset')
    preset_id = replace_id or str(uuid.uuid4())
    template, digest = import_template(data_dir, graph, model, voice, preset_id)
    preset = PresetConfig(id=preset_id, model=model, voice=voice, template=template, sha256=digest,
                         text_binding=text_binding, audio_output_node=audio_output_node,
                         cache_binding=cache_binding).model_dump(mode='json')
    proposed = copy.deepcopy(draft)
    proposed['presets'] = [p for p in proposed.get('presets', []) if p['id'] != preset_id] + [preset]
    # Validate this registration now; the entire draft is validated at save. This
    # permits repairing several bindings after changing a model cache policy.
    single_preset = copy.deepcopy(proposed)
    single_preset['presets'] = [preset]
    validate_draft(data_dir, single_preset)
    draft['presets'] = proposed['presets']
    return preset


def delete_preset(draft: dict, preset_id: str) -> None:
    draft['presets'] = [p for p in draft.get('presets', []) if p['id'] != preset_id]
    # Immutable templates may belong to last-good snapshots; never delete them here.


class _CancelAction(Exception):
    """Return from a nested picker without discarding the main draft."""


class Wizard:
    def __init__(self, data_dir: Path, *, ui=None, ask=None, secret=None, output=None):
        self.data_dir = data_dir
        self.ui = ui or UIContext()
        self.ask = ask or input
        self.secret = secret or getpass.getpass
        # Builtin print treats markup literally. Injected Rich renderers must also
        # use markup=False; every external value is sanitized before rendering.
        self.output = output or print

    def t(self, key: str, **values) -> str:
        return self.ui.t(MESSAGES, key, **values)

    def say(self, text: str):
        self.output(safe_text(text, max_length=4096))

    def error(self, error: Exception):
        self.say(diagnostic(error, self.ui))

    def choose(self, label: str, choices: list[tuple]):
        if not choices:
            raise ValueError('no_candidates')
        self.say(label)
        for number, (_, description) in enumerate(choices, 1):
            self.say(f'[{number}] {safe_text(description)}')
        self.say('[0] ' + self.t('return_menu'))
        while True:
            selected = self.ask(self.t('select_number')).strip().lower()
            if selected in ('0', 'q'):
                raise _CancelAction()
            if len(selected) <= 10 and selected.isascii() and selected.isdigit() and 1 <= int(selected) <= len(choices):
                return choices[int(selected) - 1][0]
            self.say(self.t('invalid_selection'))

    def confirm(self, key: str) -> bool:
        self.say(self.t(key))
        self.say('[1] ' + self.t('confirm_yes'))
        self.say('[0] ' + self.t('confirm_no'))
        while True:
            answer = self.ask(self.t('action')).strip().lower()
            if answer in ('1', 'y', 'yes'):
                return True
            if answer in ('', '0', 'n', 'no', 'q'):
                return False
            self.say(self.t('invalid_selection'))

    def value(self, label: str, current) -> str:
        value = self.ask(self.t('value', label=label, current=current)).strip()
        return value or str(current)

    def field(self, label: str, current, convert, validate):
        while True:
            value = self.value(label, current)
            try:
                converted = convert(value)
                validate(converted)
                return converted
            except (ValueError, TypeError) as error:
                self.error(error)

    def server(self, draft: dict):
        from .config import ServerConfig, ComfyConfig
        existing = draft.get('server', {})
        while True:
            key = self.secret(self.t('business_key')) or existing.get('api_key', '')
            try:
                ServerConfig(api_key=key)
                break
            except ValueError as error:
                self.error(error)
        host = self.field(self.t('listen_host'), existing.get('listen_host', '127.0.0.1'), str,
                          lambda value: ServerConfig(api_key=key, listen_host=value))
        port = self.field(self.t('listen_port'), existing.get('listen_port', 18388), int,
                          lambda value: ServerConfig(api_key=key, listen_host=host, listen_port=value))
        url = self.field(self.t('comfy_url'), draft.get('comfyui', {}).get('base_url', 'http://127.0.0.1:8188'),
                         str, lambda value: ComfyConfig(base_url=value))
        draft['server'] = ServerConfig(api_key=key, listen_host=host, listen_port=port).model_dump(mode='json')
        draft['comfyui'] = ComfyConfig(base_url=url).model_dump(mode='json')
        self.say(self.t('security_note'))

    def limits(self, draft: dict):
        from .config import LimitsConfig, LoggingConfig
        section = self.choose(self.t('settings'), [(key, self.t(key)) for key in ('limits', 'logging')])
        model = LimitsConfig if section == 'limits' else LoggingConfig
        values = {**model().model_dump(mode='json'), **draft.get(section, {})}
        field = self.choose(self.t('setting'), [(key, f'{self.t(key)} = {value}') for key, value in values.items()])
        previous = values[field]
        values[field] = self.field(self.t(field), previous, type(previous),
                                   lambda value: model.model_validate({**values, field: value}))
        draft[section] = model.model_validate(values).model_dump(mode='json')

    def model(self, draft: dict, *, edit: bool = False):
        from .config import IdleReleaseConfig, ModelConfig
        old = self.choose(self.t('model'), [(m['id'], m['id']) for m in draft.get('models', [])]) if edit else None
        previous = next((m for m in draft.get('models', []) if m['id'] == old), {})
        name = self.field(self.t('model_id'), old or '', str, lambda value: ModelConfig(id=value))
        policy = self.choose(self.t('cache_strategy'), [(key, self.t(key)) for key in ('inherit', 'keep', 'release', 'idle_release')])
        idle = None
        if policy == 'idle_release':
            existing = previous.get('idle_release') or {}
            # Validate seconds independently without choosing a default method for
            # the user; the temporary validator's method has no persisted effect.
            seconds = self.field(self.t('idle_seconds'), existing.get('seconds', 300), int,
                                 lambda value: IdleReleaseConfig(seconds=value, method='free'))
            method = self.choose(self.t('idle_method'), [(key, self.t('idle_' + key)) for key in ('free', 'workflow')])
            if not self.confirm('idle_' + method + '_warning'):
                return  # no partial rename, policy change, or implicit acceptance
            text = existing.get('text', '好。')
            if method == 'workflow':
                text = self.field(self.t('idle_text'), text, str,
                                  lambda value: IdleReleaseConfig(seconds=seconds, method=method, text=value))
            idle = IdleReleaseConfig(seconds=seconds, method=method, text=text).model_dump(mode='json')
        put_model(draft, name, policy, replace=old, idle_release=idle)

    def show_preset(self, preset: dict):
        self.say(f"{self.t('model_id')}: {safe_text(preset.get('model', ''))}")
        self.say(f"{self.t('voice_id')}: {safe_text(preset.get('voice', ''))}")
        for key, label in (('template', 'template'), ('text_binding', 'text_binding'),
                           ('audio_output_node', 'output_binding'), ('cache_binding', 'cache_binding')):
            if key not in preset:
                continue
            value = preset[key]
            if isinstance(value, dict):
                value = str(value.get('node_id', '')) + '.' + str(value.get('field', ''))
            self.say(f"  {self.t(label)}: {safe_text(value if value is not None else self.t('empty'))}")

    def show_summary(self, draft: dict, section: str | None = None):
        data = summary(draft)
        self.say(self.t('summary'))
        if section in (None, 'server'):
            server = data.get('server', {})
            for key in ('listen_host', 'listen_port'):
                self.say(f"{self.t(key)}: {safe_text(server.get(key, self.t('empty')))}")
            self.say(self.t('key_label') + ': ' + self.t('redacted'))
            self.say(self.t('comfy_url') + ': ' + safe_text(data.get('comfyui', {}).get('base_url', self.t('empty'))))
        if section in (None, 'models'):
            self.say(self.t('models_title'))
            for item in data.get('models', []):
                self.say(f"  {safe_text(item['id'])}: {self.t(item.get('cache_policy', 'inherit'))}")
                if item.get('cache_policy') == 'idle_release' and isinstance(item.get('idle_release'), dict):
                    idle = item['idle_release']
                    method = idle.get('method')
                    seconds = idle.get('seconds')
                    seconds = seconds if type(seconds) is int and 1 <= seconds <= 86400 else '—'
                    self.say(self.t('idle_summary', seconds=seconds,
                                    method=self.t('idle_' + method) if method in ('free', 'workflow') else self.t('empty')))
                    # Cleanup text is private config content; never include it in summaries.
            if not data.get('models'):
                self.say(self.t('empty'))
        if section in (None, 'presets'):
            self.say(self.t('presets_title'))
            for preset in data.get('presets', []):
                self.show_preset(preset)
            if not data.get('presets'):
                self.say(self.t('empty'))
        if section in (None, 'limits'):
            from .config import LimitsConfig, LoggingConfig
            for group, model in (('limits', LimitsConfig), ('logging', LoggingConfig)):
                self.say(self.t(group))
                for key, value in {**model().model_dump(mode='json'), **data.get(group, {})}.items():
                    self.say(f'  {self.t(key)}: {value}')

    def preset(self, draft: dict, *, edit: bool = False):
        from .config import validate_identifier
        old = self.choose(self.t('preset'), [(p, f"{p['model']} / {p['voice']}") for p in draft.get('presets', [])]) if edit else None
        model = self.choose(self.t('model'), [(m['id'], m['id']) for m in draft.get('models', [])])
        voice = self.field(self.t('voice_id'), old['voice'] if old else '', str, validate_identifier)
        while True:
            source = self.ask(self.t('workflow_file')).strip().strip('"')
            if source.lower() in ('0', 'q'):
                raise _CancelAction()
            try:
                graph = read_import(self.data_dir / old['template'] if not source and old else Path(source))
                break
            except (ValueError, OSError) as error:
                self.error(error)
        self.say(self.t('import_note'))
        while True:
            text = self.choose(self.t('select_text'), binding_choices(graph))
            output_node = self.choose(self.t('select_output'), node_choices(graph))
            cache = self.choose(self.t('select_cache'), [(None, self.t('no_cache'))] + binding_choices(graph, cache=True))
            self.show_preset({'model': model, 'voice': voice, 'text_binding': text,
                              'audio_output_node': output_node, 'cache_binding': cache})
            if not self.confirm('apply_preset'):
                return
            try:
                put_preset(self.data_dir, draft, graph, model=model, voice=voice, text_binding=text,
                           audio_output_node=output_node, cache_binding=cache, replace_id=old['id'] if old else None)
                return
            except (ValueError, OSError) as error:
                self.error(error)
                # Keep chosen source/model/voice and allow correcting explicit bindings.
                if str(error) == 'duplicate_preset_choose_edit':
                    return

    def save(self, draft: dict, original: bytes | None) -> int | None:
        from .config import save_config
        from .control import get_status, config_digest
        config = validate_draft(self.data_dir, draft)
        self.show_summary(draft)
        if not config.presets:
            self.say(self.t('deferred'))
        self.say(self.t('save_note'))
        if not self.confirm('confirm_save'):
            return None
        path = self.data_dir / 'config.json'
        current = path.read_bytes() if path.exists() else None
        if current != original:
            self.say(self.t('external_change'))
            return 1
        save_config(self.data_dir, config)
        status = get_status(self.data_dir)
        self.say(self.t('saved_current' if status and status.get('config_digest') == config_digest(config) else 'saved_pending'))
        return 0

    def return_from_editor(self, draft: dict, baseline: dict, original: bytes | None) -> int | None:
        if draft == baseline:
            return 1
        self.say(self.t('dirty'))
        for key, label in (('1', 'dirty_save'), ('2', 'dirty_discard'), ('0', 'dirty_continue')):
            self.say(f'[{key}] {self.t(label)}')
        while True:
            answer = self.ask(self.t('action')).strip().lower()
            if answer in ('1', 's'):
                return self.save(draft, original)
            if answer == '2':
                return 1
            if answer in ('0', 'q'):
                return None
            self.say(self.t('invalid_selection'))

    def menu(self, section: str | None) -> dict[str, str]:
        maps = {
            None: [('1', 'summary'), ('2', 'edit_server'), ('3', 'add_model'), ('4', 'edit_model'),
                   ('5', 'delete_model'), ('6', 'add_preset'), ('7', 'edit_preset'), ('8', 'delete_preset'), ('9', 'edit_limits')],
            'server': [('1', 'summary'), ('2', 'edit_server')],
            'models': [('1', 'list'), ('2', 'add_model'), ('3', 'edit_model'), ('4', 'delete_model')],
            'presets': [('1', 'list'), ('2', 'add_preset'), ('3', 'edit_preset'), ('4', 'delete_preset')],
            'limits': [('1', 'summary'), ('2', 'edit_limits')],
        }
        options = dict(maps[section] + [('s', 'save_menu'), ('0', 'return_menu')])
        self.say(self.t(section + '_title' if section else 'editor_title'))
        for key, label in options.items():
            self.say(f'[{key.upper()}] {self.t(label)}')
        return options

    def run(self, *, section: str | None = None, first_run: bool = False) -> int:
        from .config import load_config
        if section not in (None, 'server', 'models', 'presets', 'limits'):
            raise ValueError('invalid_wizard_section')
        with InstanceLock(self.data_dir, 'config'):
            path = self.data_dir / 'config.json'
            original = path.read_bytes() if path.exists() else None
            draft = {'schema_version': 1, 'models': [], 'presets': []}
            valid_existing = False
            try:
                draft = load_config(self.data_dir).model_dump(mode='json')
                valid_existing = True
            except (ValueError, OSError) as error:
                if original is not None:
                    self.say(self.t('invalid_existing'))
                    self.error(error)
            baseline = copy.deepcopy(draft)
            if first_run:
                self.say(self.t('first_title'))
            if first_run or not valid_existing:
                self.server(draft)
            if first_run:
                if self.confirm('first_model'):
                    try:
                        self.model(draft)
                    except _CancelAction:
                        pass
                    except (ValueError, OSError) as error:
                        self.error(error)
                if draft.get('models') and self.confirm('first_preset'):
                    try:
                        self.preset(draft)
                    except _CancelAction:
                        pass
                    except (ValueError, OSError) as error:
                        self.error(error)
                try:
                    result = self.save(draft, original)
                    if result is not None:
                        return result
                except (ValueError, OSError) as error:
                    self.error(error)
                self.say(self.t('first_cancel_save'))
                section = None  # all retained draft fields remain repairable
            while True:
                options = self.menu(section)
                action = self.ask(self.t('action')).strip().lower()
                try:
                    if action in ('0', 'q'):
                        result = self.return_from_editor(draft, baseline, original)
                        if result is not None:
                            return result
                        continue
                    operation = options.get(action)
                    if operation in ('summary', 'list'):
                        self.show_summary(draft, section)
                    elif operation == 'edit_server':
                        self.server(draft)
                    elif operation in ('add_model', 'edit_model'):
                        self.model(draft, edit=operation == 'edit_model')
                    elif operation == 'delete_model':
                        model = self.choose(self.t('delete_model'), [(m['id'], m['id']) for m in draft.get('models', [])])
                        if self.confirm('confirm_delete_model'):
                            delete_model(draft, model, cascade=True)
                    elif operation in ('add_preset', 'edit_preset'):
                        self.preset(draft, edit=operation == 'edit_preset')
                    elif operation == 'delete_preset':
                        preset = self.choose(self.t('delete_preset'), [(p['id'], f"{p['model']} / {p['voice']}") for p in draft.get('presets', [])])
                        if self.confirm('confirm_delete_preset'):
                            delete_preset(draft, preset)
                    elif operation == 'edit_limits':
                        self.limits(draft)
                    elif operation == 'save_menu':
                        result = self.save(draft, original)
                        if result is not None:
                            return result
                    else:
                        self.say(self.t('invalid_selection'))
                except _CancelAction:
                    continue
                except (ValueError, OSError, KeyError, TypeError) as error:
                    self.error(error)
                    self.say(self.t('not_saved'))


def run_wizard(data_dir: Path, *, ui=None, section=None, first_run=False, **io) -> int:
    try:
        return Wizard(data_dir, ui=ui, **io).run(section=section, first_run=first_run)
    except (KeyboardInterrupt, EOFError):
        # No implicit save on terminal close; all prior on-disk configuration stays intact.
        return 1
