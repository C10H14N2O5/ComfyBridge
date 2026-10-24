"""Observer only: Q, Ctrl-C and window close never stop backend/controller."""
from __future__ import annotations

import os
from pathlib import Path
import re
import time

from .control import config_digest, get_status
from .config import parse_json_strict
from .console_input import ConsoleInput
from .i18n import UIContext, safe_text


MESSAGES = {
    'title': ('ComfyBridge 实时监控', 'ComfyBridge Monitor'),
    'observer_notice': ('关闭窗口、Q 或 Ctrl-C 仅关闭监控，不会停止后台。', 'Closing this window, Q or Ctrl-C closes the observer, not the backend.'),
    'embedded_notice': ('0、Q 或 Ctrl-C 返回主菜单，不会停止后台。', '0, Q or Ctrl-C returns to the home menu without stopping the backend.'),
    'backend': ('后台：{value}', 'Backend: {value}'),
    'backend_confirmed': ('已验证连接', 'verified connection'),
    'backend_unknown': ('未确认连接（可能未运行或无法验证身份）；不会自动重启', 'connection unconfirmed (may be stopped or identity unavailable); no automatic restart'),
    'online': ('ComfyUI 连接：{value}', 'ComfyUI connection: {value}'),
    'ready': ('语音服务就绪：{value}', 'Speech service ready: {value}'),
    'draining': ('正在排空：{value}', 'Draining: {value}'),
    'drained': ('已安全排空：{value}', 'Safely drained: {value}'),
    'yes': ('是', 'yes'), 'no': ('否', 'no'), 'unknown': ('未知', 'unknown'),
    'connected': ('在线', 'online'), 'disconnected': ('离线', 'offline'),
    'counts': ('任务：远端在途 {inflight}；本地等待 {waiting}；音频发送 {delivery}；已占名额 {slots}', 'Tasks: remote in flight {inflight}; waiting {waiting}; audio delivery {delivery}; occupied slots {slots}'),
    'saved_config': ('已保存配置：{value}', 'Saved configuration: {value}'),
    'config_current': ('与运行配置一致', 'matches the running configuration'),
    'config_pending': ('等待重启生效', 'pending restart'),
    'config_invalid': ('缺失或无效，需要修复', 'missing or invalid; repair required'),
    'restart': ('重启控制器：{value}', 'Restart controller: {value}'),
    'restart_error': ('重启提示：{value}', 'Restart detail: {value}'),
    'presets': ('音色预设：', 'Voice presets:'),
    'preset': ('  {model} / {voice}：就绪 {ready}{reasons}', '  {model} / {voice}: ready {ready}{reasons}'),
    'reasons': ('；原因：{value}', '; reason: {value}'),
    'no_presets': ('尚无已登记预设；不能接收语音请求。', 'No presets registered; speech requests cannot be accepted.'),
    'unresolved': ('未解决的远端任务：{count}', 'Unresolved remote tasks: {count}'),
    'task': ('  {task_id}：{state}；调用端 {client}{reason}', '  {task_id}: {state}; client {client}{reason}'),
    'more': ('  另有 {count} 项未显示。', '  {count} more entries not shown.'),
    'logs': ('最近日志（仅安全元数据）：', 'Recent logs (safe metadata only):'),
    'no_logs': ('暂无可显示的日志。', 'No recent logs available.'),
    'log_omitted': ('无法识别的日志条目（详情已省略）', 'Unrecognized log entry (details omitted)'),
    'controls': ('操作：', 'Actions:'),
    'start_action': ('[1 / S] 启动或连接后台', '[1 / S] Start or attach backend'),
    'stop_action': ('[2 / X] 排空并停止后台', '[2 / X] Drain and stop backend'),
    'restart_action': ('[3 / R] 重启后台', '[3 / R] Restart backend'),
    'configure_action': ('[4 / C] 编辑配置', '[4 / C] Configure'),
    'force_action': ('[5 / F] 强制停止 Bridge（不停止远端任务）', '[5 / F] Force stop Bridge (remote tasks continue)'),
    'close_action': ('[0 / Q] 关闭监控', '[0 / Q] Close monitor'),
    'return_action': ('[0 / Q] 返回主菜单', '[0 / Q] Return to home menu'),
    'stop_confirm': ('排空并停止后台？[1] 确认 / [0] 取消（Y/N）：', 'Drain and stop the backend? [1] Confirm / [0] Cancel (Y/N): '),
    'force_confirm': ('仅强制停止 Bridge；远端任务可能继续，任务记录将保留。[1] 确认 / [0] 取消（Y/N）：', 'Force stop ONLY Bridge; remote tasks may continue and the journal is retained. [1] Confirm / [0] Cancel (Y/N): '),
    'restart_confirm': ('排空并重启后台以应用已保存配置？独立控制器将在离开监控后继续执行。[1] 确认 / [0] 取消（Y/N，默认取消）：', 'Drain and restart the backend with the saved configuration? The independent controller continues after leaving the monitor. [1] Confirm / [0] Cancel (Y/N; default cancel): '),
    'started': ('后台已启动或已连接。', 'Backend started or attached.'),
    'stop_requested': ('已请求停止；仍可用 0/Q 独立离开监控。', 'Stop requested; 0/Q still closes only this observer.'),
    'restart_requested': ('已启动独立重启控制器；离开监控不会中止重启。', 'Independent restart controller started; leaving the monitor does not cancel it.'),
    'cancelled': ('操作已取消。', 'Operation cancelled.'),
    'saved': ('配置已保存；重启后生效，不会自动重启。', 'Configuration saved; a separate restart is required.'),
    'not_saved': ('配置未保存。', 'Configuration not saved.'),
    'operation_failed': ('操作失败；请检查配置及脱敏日志。', 'Operation failed; check configuration and sanitized logs.'),
    'state_idle': ('空闲', 'idle'), 'state_draining': ('等待任务安全结束', 'waiting for tasks to drain'),
    'state_starting': ('正在启动', 'starting'), 'state_fallback_starting': ('正在启动上次可用配置', 'starting last-known-good configuration'),
    'state_fallback_running': ('已回退至上次可用配置', 'running last-known-good configuration'),
    'state_completed': ('已完成', 'completed'), 'state_cancelled': ('已取消', 'cancelled'),
    'state_failed': ('失败', 'failed'), 'state_invalid': ('状态记录无效', 'invalid status record'),
    'state_intent': ('提交意图已记录', 'submission intent recorded'), 'state_uncertain': ('状态不确定（不会自动重放）', 'uncertain (never automatically replayed)'),
    'state_pending': ('远端排队中', 'pending remotely'), 'state_running': ('远端执行中', 'running remotely'),
    'state_success': ('成功', 'success'), 'state_error': ('失败', 'error'), 'state_not_active': ('已确认不再活动', 'confirmed no longer active'),
    'state_resolved': ('管理员已确认', 'acknowledged by administrator'),
    'state_active': ('等待响应', 'awaiting response'), 'state_abandoned': ('已离开', 'disconnected or abandoned'), 'state_delivered': ('已发送', 'delivered'),
    'code_remote_unavailable': ('无法连接 ComfyUI', 'ComfyUI unavailable'),
    'code_submission_uncertain': ('提交结果不确定', 'submission outcome uncertain'),
    'code_remote_state_unknown': ('远端状态未知', 'remote state unknown'),
    'code_remote_execution_failed': ('远端执行失败', 'remote execution failed'),
    'code_deadline_exceeded': ('请求已超时', 'request deadline exceeded'),
    'code_not_probed': ('尚未检查远端', 'remote readiness not checked yet'),
    'code_submission_rejected': ('远端拒绝提交', 'submission rejected by remote'),
    'code_remote_not_active': ('远端任务不再活动', 'remote task no longer active'),
    'code_internal_error': ('内部错误（详情已省略）', 'internal error (details omitted)'),
    'code_local_cleanup_failed': ('本地临时文件暂未清理', 'local temporary-file cleanup deferred'),
    'code_schema_missing_class': ('缺少所需节点类型', 'required node class missing'),
    'code_schema_missing_node': ('缺少所需节点', 'required node missing'),
    'code_schema_invalid': ('远端节点接口无效', 'invalid remote node schema'),
    'code_schema_invalid_graph': ('工作流结构无效', 'invalid workflow graph'),
    'code_schema_missing_required_input': ('缺少节点必填输入', 'required node input missing'),
    'code_schema_unknown_input': ('节点含未知输入', 'unknown node input'),
    'code_schema_output_slot': ('输出编号不兼容', 'output slot incompatible'),
    'code_schema_link_type': ('节点连接类型不兼容', 'node link type incompatible'),
    'code_schema_combo_value': ('所选节点参数不可用', 'selected node option unavailable'),
    'code_schema_combo_multiselect_unsupported': ('不支持多选节点参数', 'multi-select node option unsupported'),
    'code_schema_input_type': ('节点输入类型不兼容', 'node input type incompatible'),
    'code_schema_input_range': ('节点输入超出范围', 'node input out of range'),
    'code_schema_input_requires_link': ('节点输入必须使用连接', 'node input requires a link'),
    'code_schema_text_binding': ('文本输入绑定不兼容', 'text input binding incompatible'),
    'code_schema_cache_binding': ('缓存控制绑定不兼容', 'cache-control binding incompatible'),
    'code_schema_audio_output': ('音频输出不兼容', 'audio output incompatible'),
    'code_pending_config_start_failed': ('新配置启动失败，已尝试回退', 'new configuration failed to start; fallback attempted'),
    'code_restart_failed_check_local_config_and_logs': ('重启失败，请检查配置与日志', 'restart failed; check configuration and logs'),
    'event_service_started': ('服务已启动', 'Service started'),
    'event_submission_intent': ('已记录提交意图', 'Submission intent recorded'),
    'event_submission_acknowledged': ('远端已接收任务', 'Remote task acknowledged'),
    'event_submission_rejected': ('远端拒绝提交', 'Submission rejected'),
    'event_submission_uncertain': ('提交结果不确定', 'Submission outcome uncertain'),
    'event_pending_delete_requested': ('已请求删除本服务的排队任务', 'Owned pending-task deletion requested'),
    'event_remote_terminal': ('远端任务已结束', 'Remote task reached terminal state'),
    'event_cleanup_deferred': ('临时文件清理已延后', 'Temporary-file cleanup deferred'),
    'event_worker_failed': ('后台处理失败', 'Worker failed'),
    'event_diagnostic_details_omitted': ('诊断详情已省略', 'Diagnostic details omitted'),
    'level_INFO': ('信息', 'INFO'), 'level_WARNING': ('警告', 'WARNING'), 'level_ERROR': ('错误', 'ERROR'),
    'level_DEBUG': ('调试', 'DEBUG'), 'level_CRITICAL': ('严重', 'CRITICAL'),
    'field_request_id': ('请求', 'request'), 'field_prompt_id': ('任务', 'task'), 'field_instance_id': ('实例', 'instance'),
    'field_model': ('模型', 'model'), 'field_voice': ('音色', 'voice'), 'field_input_length': ('字符数', 'characters'),
    'field_elapsed_seconds': ('耗时秒', 'elapsed seconds'), 'field_queue_seconds': ('排队秒', 'queue seconds'),
    'field_generation_seconds': ('生成秒', 'generation seconds'), 'field_count': ('数量', 'count'),
    'field_pid': ('进程号', 'PID'), 'field_code': ('代码', 'code'), 'field_state': ('状态', 'state'),
    'cache_title': ('模型空闲缓存清理：', 'Model idle-cache cleanup:'),
    'cache_row': ('  {model}{voice}：{method}；等待 {seconds} 秒；剩余 {remaining} 秒', '  {model}{voice}: {method}; idle timeout {seconds}s; remaining {remaining}s'),
    'cache_voice': (' / 最近音色 {voice}', ' / last voice {voice}'),
    'cache_detail': ('    状态：{state}{reason}', '    State: {state}{reason}'),
    'cache_method_free': ('/free 实例级卸载请求', '/free instance-wide unload request'),
    'cache_method_workflow': ('工作流 GPU 清理任务', 'workflow GPU cleanup task'),
    'cache_state_pending_idle': ('等待本地任务空闲', 'waiting for local work to become idle'),
    'cache_state_countdown': ('空闲倒计时', 'idle countdown'),
    'cache_state_waiting_remote': ('等待远端队列为空（无法锁定其他客户端）', 'waiting for an empty remote queue (other clients cannot be locked)'),
    'cache_state_releasing': ('清理请求或任务进行中', 'cleanup request or task in progress'),
    'cache_state_requested': ('请求已受理（不证明显存已释放）', 'request accepted (does not prove VRAM was freed)'),
    'cache_state_completed': ('清理工作流已完成（不证明显存已释放）', 'cleanup workflow completed (does not prove VRAM was freed)'),
    'cache_state_failed': ('清理失败', 'cleanup failed'),
    'cache_state_uncertain': ('清理结果不确定（不会盲目重发）', 'cleanup outcome uncertain (not blindly retried)'),
    'cache_state_stale': ('配置或模板已变化；等待新的业务生成建立匹配的清理计划，不会仅靠自动探测恢复', 'configuration or template changed; waiting for a new business generation to establish a matching cleanup plan, not automatic probe recovery'),
    'code_cache_remote_busy': ('远端队列仍有任务', 'remote queue still contains work'),
    'code_cache_remote_unavailable': ('无法核对远端队列', 'remote queue could not be checked'),
    'code_cache_config_stale': ('配置或模板已变化', 'configuration or template has changed'),
    'code_cache_build_failed': ('无法构建清理工作流', 'cleanup workflow could not be built'),
    'code_cache_seed_exhausted': ('持久化清理种子序列已耗尽或无效，未提交清理任务', 'durable cleanup seed sequence exhausted or invalid; no cleanup task submitted'),
    'code_cache_free_uncertain': ('/free 请求结果不确定', '/free request outcome uncertain'),
    'code_cache_release_uncertain': ('缓存清理结果不确定', 'cache cleanup outcome uncertain'),
    'code_cache_free_failed': ('/free 请求失败', '/free request failed'),
    'code_cache_cleanup_failed': ('清理工作流失败', 'cleanup workflow failed'),
    'code_cache_deadline_exceeded': ('清理任务超过时限', 'cleanup task deadline exceeded'),
    'code_cache_not_active': ('清理任务已确认不再活动', 'cleanup task confirmed no longer active'),
    'code_cache_admin_resolved': ('管理员已确认清理任务状态', 'cleanup task acknowledged by administrator'),
    'code_workflow_idle_release_settings': ('工作流定时丢弃设置不一致', 'workflow idle-release settings are inconsistent'),
    'code_cache_cleanup_unsupported': ('此模板不支持工作流清理', 'this template does not support workflow cleanup'),
    'code_cache_cleanup_seed_binding': ('清理 seed 必须是有效整数常量或省略', 'cleanup seed must be a valid literal integer or omitted'),
    'code_cache_cleanup_seed_invalid': ('清理种子无效或已耗尽', 'cleanup seed invalid or exhausted'),
    'code_cache_cleanup_text': ('清理任务文本无效', 'cleanup task text invalid'),
    'code_cache_cleanup_seed_schema': ('远端缺少兼容的 INT seed 输入', 'remote node lacks a compatible INT seed input'),
    'code_cache_cleanup_seed_range': ('远端 seed 不支持完整 32 位范围', 'remote seed does not support the complete 32-bit range'),
    'event_cache_release_intent': ('已记录缓存清理提交意图', 'Cache cleanup submission intent recorded'),
    'event_cache_release_requested': ('缓存清理请求已受理（不证明显存已释放）', 'Cache cleanup request accepted (not proof of freed VRAM)'),
    'event_cache_release_completed': ('缓存清理工作流已完成（不证明显存已释放）', 'Cache cleanup workflow completed (not proof of freed VRAM)'),
    'event_cache_release_failed': ('缓存清理失败', 'Cache cleanup failed'),
    'event_cache_release_uncertain': ('缓存清理结果不确定', 'Cache cleanup outcome uncertain'),
}


_CODE = re.compile(r'[a-z][a-z0-9_]{0,95}\Z', re.ASCII)
_TOKEN = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z', re.ASCII)


def _localized_code(value, ui, prefix='code') -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        return ui.t(MESSAGES, 'unknown')
    key = prefix + '_' + value
    return ui.t(MESSAGES, key) if key in MESSAGES else safe_text(value, max_length=96)


def _boolean(value, ui, *, connection=False) -> str:
    key = ('connected' if value else 'disconnected') if connection else ('yes' if value else 'no')
    return ui.t(MESSAGES, key if type(value) is bool else 'unknown')


def _count(value) -> str:
    return str(value) if type(value) is int and 0 <= value <= 10**12 else '—'


def _label(value) -> str:
    return safe_text(value, max_length=128) if isinstance(value, str) else '—'


_CACHE_STATES = frozenset({'pending_idle', 'countdown', 'waiting_remote', 'releasing', 'requested',
                           'completed', 'failed', 'uncertain', 'stale'})
_CACHE_ERRORS = frozenset({'cache_remote_busy', 'cache_remote_unavailable', 'cache_config_stale',
                          'cache_build_failed', 'cache_free_uncertain', 'cache_free_failed',
                          'cache_cleanup_failed', 'cache_deadline_exceeded', 'cache_not_active', 'cache_admin_resolved',
                          'cache_release_uncertain', 'submission_uncertain', 'cache_seed_exhausted'})
_CACHE_EVENTS = frozenset({'cache_release_intent', 'cache_release_requested', 'cache_release_completed',
                          'cache_release_failed', 'cache_release_uncertain'})
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z', re.ASCII)


def _cache_value(value, ui, prefix: str, allowed) -> str:
    if not isinstance(value, str) or value not in allowed:
        return ui.t(MESSAGES, 'unknown')
    return ui.t(MESSAGES, prefix + '_' + value)


def _cache_identifier(value) -> str:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else '—'


def format_cache_status(rows, ui: UIContext) -> list[str]:
    """Closed schema: never echo text, arbitrary error/state/method strings or dicts."""
    if not isinstance(rows, list) or not rows:
        return []
    lines = [ui.t(MESSAGES, 'cache_title')]
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        method = row.get('method')
        method_text = _cache_value(method, ui, 'cache_method', ('free', 'workflow'))
        seconds = row.get('seconds')
        seconds = str(seconds) if type(seconds) is int and 1 <= seconds <= 86400 else '—'
        remaining = row.get('remaining_seconds')
        remaining = f'{remaining:.1f}' if type(remaining) in (int, float) and 0 <= remaining <= 86400 else '—'
        voice = _cache_identifier(row.get('voice'))
        lines.append(ui.t(MESSAGES, 'cache_row', model=_cache_identifier(row.get('model')),
                          voice=ui.t(MESSAGES, 'cache_voice', voice=voice) if voice != '—' else '',
                          method=method_text, seconds=seconds, remaining=remaining))
        state = row.get('state')
        if state == 'completed':
            # /free only acknowledges an instance-wide request. Even a malformed
            # completed state must never upgrade that to confirmed unloading.
            state = 'requested' if method == 'free' else ('completed' if method == 'workflow' else None)
        state_text = _cache_value(state, ui, 'cache_state', _CACHE_STATES)
        error = row.get('error_code')
        reason = ui.t(MESSAGES, 'reasons', value=_cache_value(error, ui, 'code', _CACHE_ERRORS)) if error is not None else ''
        lines.append(ui.t(MESSAGES, 'cache_detail', state=state_text, reason=reason))
    if len(rows) > 20:
        lines.append(ui.t(MESSAGES, 'more', count=len(rows) - 20))
    return lines


def format_status(status: dict | None, ui: UIContext) -> list[str]:
    """Present only recognized status fields; never serialize arbitrary status payloads."""
    state = status if isinstance(status, dict) else {}
    backend = state.get('backend', state)
    backend = backend if isinstance(backend, dict) else {}
    confirmed = any(type(backend.get(key)) is bool for key in ('ready', 'online', 'draining'))
    lines = [ui.t(MESSAGES, 'backend', value=ui.t(MESSAGES, 'backend_confirmed' if confirmed else 'backend_unknown'))]
    if confirmed:
        lines.extend([
            ui.t(MESSAGES, 'online', value=_boolean(backend.get('online'), ui, connection=True)),
            ui.t(MESSAGES, 'ready', value=_boolean(backend.get('ready'), ui)),
            ui.t(MESSAGES, 'draining', value=_boolean(backend.get('draining'), ui)),
            ui.t(MESSAGES, 'counts', **{key: _count(backend.get(key)) for key in ('inflight', 'waiting', 'delivery', 'slots')}),
        ])
        if 'drained' in backend:
            lines.append(ui.t(MESSAGES, 'drained', value=_boolean(backend.get('drained'), ui)))
    config_keys = {'current': 'config_current', 'saved_pending_restart': 'config_pending', 'invalid_or_missing': 'config_invalid'}
    saved = state.get('saved_config')
    if isinstance(saved, str) and saved in config_keys:
        lines.append(ui.t(MESSAGES, 'saved_config', value=ui.t(MESSAGES, config_keys[saved])))
    restart = state.get('restart')
    if isinstance(restart, dict):
        lines.append(ui.t(MESSAGES, 'restart', value=_localized_code(restart.get('state'), ui, 'state')))
        if restart.get('error'):
            lines.append(ui.t(MESSAGES, 'restart_error', value=_localized_code(restart['error'], ui)))
    presets = backend.get('presets')
    if isinstance(presets, list):
        lines.append(ui.t(MESSAGES, 'presets' if presets else 'no_presets'))
        for preset in presets[:20]:
            if not isinstance(preset, dict):
                continue
            reasons = preset.get('reasons')
            reasons = ', '.join(_localized_code(code, ui) for code in reasons[:8]) if isinstance(reasons, list) else ''
            lines.append(ui.t(MESSAGES, 'preset', model=_label(preset.get('model')), voice=_label(preset.get('voice')),
                              ready=_boolean(preset.get('ready'), ui),
                              reasons=ui.t(MESSAGES, 'reasons', value=reasons) if reasons else ''))
        if len(presets) > 20:
            lines.append(ui.t(MESSAGES, 'more', count=len(presets) - 20))
    lines.extend(format_cache_status(backend.get('cache'), ui))
    unresolved = backend.get('unresolved')
    if isinstance(unresolved, list):
        lines.append(ui.t(MESSAGES, 'unresolved', count=len(unresolved)))
        for task in unresolved[:20]:
            if not isinstance(task, dict):
                continue
            code = task.get('error_code')
            lines.append(ui.t(MESSAGES, 'task', task_id=_label(task.get('task_id')),
                              state=_localized_code(task.get('state'), ui, 'state'),
                              client=_localized_code(task.get('client_state'), ui, 'state'),
                              reason=ui.t(MESSAGES, 'reasons', value=_localized_code(code, ui)) if code else ''))
        if len(unresolved) > 20:
            lines.append(ui.t(MESSAGES, 'more', count=len(unresolved) - 20))
    return lines


def format_log_line(line: str, ui: UIContext) -> str:
    """Read our structured log schema only, omitting unknown fields/raw messages."""
    try:
        if not isinstance(line, str) or len(line) > 4096:
            raise ValueError()
        record = parse_json_strict(line)
        if not isinstance(record, dict) or not isinstance(record.get('event'), str) or not _CODE.fullmatch(record['event']):
            raise ValueError()
    except ValueError:
        return ui.t(MESSAGES, 'log_omitted')
    cache_event = record['event'] in _CACHE_EVENTS
    if record['event'].startswith('cache_') and not cache_event:
        return ui.t(MESSAGES, 'log_omitted')
    parts = []
    timestamp = record.get('time')
    if isinstance(timestamp, str) and re.fullmatch(r'[0-9TZ:+. -]{1,40}', timestamp):
        parts.append(timestamp)
    level_key = 'level_' + record.get('level', '') if isinstance(record.get('level', ''), str) else ''
    if level_key in MESSAGES:
        parts.append(ui.t(MESSAGES, level_key))
    parts.append(_localized_code(record['event'], ui, 'event'))
    for field in ('request_id', 'prompt_id', 'model', 'voice', 'input_length', 'elapsed_seconds',
                  'queue_seconds', 'generation_seconds', 'code', 'state', 'count', 'pid', 'instance_id'):
        value = record.get(field)
        if value is None:
            continue
        if cache_event and field in ('code', 'state'):
            value = _cache_value(value, ui, 'code' if field == 'code' else 'cache_state',
                                 _CACHE_ERRORS if field == 'code' else _CACHE_STATES)
        elif field in ('code', 'state'):
            value = _localized_code(value, ui, field)
        elif type(value) in (int, float) and -10**12 <= value <= 10**12:
            value = str(value)
        elif isinstance(value, str) and _TOKEN.fullmatch(value):
            value = safe_text(value, max_length=128)
        else:
            continue
        parts.append(ui.t(MESSAGES, 'field_' + field) + '=' + value)
    return ' | '.join(parts)


def tail_lines(path: Path, count: int = 50, max_bytes: int = 65536) -> list[str]:
    if count <= 0 or max_bytes <= 0:
        return []
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            text = stream.read(max_bytes).decode('utf-8', errors='replace')
        lines = text.splitlines()
        if size > max_bytes:
            lines = lines[1:]
        # Prevent terminal escape injection even from a manually altered log file.
        return [re.sub(r'[\x00-\x1f\x7f-\x9f]', '', line)[:4096] for line in lines[-count:]]
    except OSError:
        return []


def monitor_snapshot(data_dir: Path) -> dict:
    from .launcher import _controller_status
    status = get_status(data_dir)
    pending = 'invalid_or_missing'
    try:
        from .config import load_config
        digest = config_digest(load_config(data_dir))
        pending = 'current' if status and digest == status.get('config_digest') else 'saved_pending_restart'
    except (OSError, ValueError):
        pass
    return {'backend': status or {'state': 'not_running_no_auto_restart'},
            'saved_config': pending, **_controller_status(data_dir)}


def _windows_redirected_key(stream) -> str | None:
    """Read at most one byte, and never wait on a pipe with an open writer."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    try:
        fd = stream.fileno()
        handle = msvcrt.get_osfhandle(fd)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetFileType.argtypes = [wintypes.HANDLE]
        kernel.GetFileType.restype = wintypes.DWORD
        kind = kernel.GetFileType(handle)
        if kind == 3:  # FILE_TYPE_PIPE: peek before os.read, never blocking readline.
            kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
            kernel.PeekNamedPipe.restype = wintypes.BOOL
            available = wintypes.DWORD()
            if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                return 'q'  # broken/disconnected/invalid input: close observer only
            if not available.value:
                return None
        elif kind != 1:  # FILE_TYPE_DISK can be read directly; NUL/no handle is EOF.
            return 'q'
        value = os.read(fd, 1)
        return value.decode('ascii', errors='ignore').strip().lower() or (None if value else 'q')
    except (OSError, ValueError, AttributeError):
        return 'q'


def read_key() -> str | None:
    import sys
    if sys.stdin is None or sys.stdin.closed:
        return 'q'
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        # Windows NUL can report isatty=True; only GetConsoleMode proves this
        # handle is a console input buffer rather than a character device.
        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetConsoleMode.restype = wintypes.BOOL
            mode = wintypes.DWORD()
            is_console = kernel.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode))
        except (OSError, ValueError, AttributeError):
            return 'q'
        if not is_console:
            return _windows_redirected_key(sys.stdin)
        return msvcrt.getwch().lower() if msvcrt.kbhit() else None
    import select
    if select.select([sys.stdin], [], [], 0)[0]:
        # One byte also prevents partial piped input from blocking POSIX refresh.
        value = os.read(sys.stdin.fileno(), 1)
        return value.decode('ascii', errors='ignore').strip().lower() or (None if value else 'q')
    return None


def monitor(data_dir: Path, *, ui: UIContext | None = None, return_to_menu: bool = False,
            refresh: float = 2, log_lines: int = 50, key_reader=None, output=None, confirm=None, reader=None) -> int:
    from rich.console import Console
    from .launcher import ensure_backend, request_restart, request_stop
    ui = ui if ui is not None else UIContext()
    reader = reader if reader is not None else ConsoleInput()
    key_reader = key_reader or reader.poll_key
    console = Console()
    clear_screen = output is None and console.is_terminal
    output = output or (lambda text: console.print(text, markup=False, highlight=False))
    confirm = confirm or (lambda prompt: reader.ask(prompt).strip().lower() in ('1', 'y'))
    aliases = {'1': 's', '2': 'x', '3': 'r', '4': 'c', '5': 'f'}
    message_key = None
    try:
        while True:
            if clear_screen:
                console.clear()
            try:
                state = monitor_snapshot(data_dir)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                state = None
            output(ui.t(MESSAGES, 'title'))
            output(ui.t(MESSAGES, 'embedded_notice' if return_to_menu else 'observer_notice'))
            for line in format_status(state, ui):
                output(line)
            output(ui.t(MESSAGES, 'logs'))
            lines = tail_lines(data_dir / 'logs/comfybridge.log', log_lines)
            for line in lines:
                output(format_log_line(line, ui))
            if not lines:
                output(ui.t(MESSAGES, 'no_logs'))
            if message_key:
                output(ui.t(MESSAGES, message_key))
            for key in ('controls', 'start_action', 'stop_action', 'restart_action', 'configure_action', 'force_action',
                        'return_action' if return_to_menu else 'close_action'):
                output(ui.t(MESSAGES, key))
            deadline = time.monotonic() + max(0.05, refresh)
            while time.monotonic() < deadline:
                key = key_reader()
                key = key.strip().lower() if isinstance(key, str) else None
                if key in ('q', '0'):
                    return 0
                key = aliases.get(key, key)
                if key in ('s', 'x', 'f', 'r', 'c'):
                    try:
                        if key == 's':
                            ensure_backend(data_dir)
                            message_key = 'started'
                        elif key in ('x', 'f'):
                            warning = ui.t(MESSAGES, 'force_confirm' if key == 'f' else 'stop_confirm')
                            if confirm(warning):
                                request_stop(data_dir, force=key == 'f')
                                message_key = 'stop_requested'
                            else:
                                message_key = 'cancelled'
                        elif key == 'r':
                            if confirm(ui.t(MESSAGES, 'restart_confirm')):
                                request_restart(data_dir)
                                message_key = 'restart_requested'
                            else:
                                message_key = 'cancelled'
                        elif key == 'c':
                            from .wizard import run_wizard
                            saved = run_wizard(data_dir, ui=ui, ask=reader.ask, secret=reader.secret, output=output)
                            message_key = 'saved' if saved == 0 else 'not_saved'
                    except (OSError, ValueError, RuntimeError, TimeoutError):
                        message_key = 'operation_failed'
                    break
                time.sleep(.05)
    except (KeyboardInterrupt, EOFError):
        return 0
