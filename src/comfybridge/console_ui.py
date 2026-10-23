"""Bilingual home screen: an observer/controller, never a second backend."""
from __future__ import annotations

from pathlib import Path
import uuid

from . import __version__
from .config import load_config
from .console_input import ConsoleInput
from .control import ControlClient, config_digest, get_status
from .i18n import UIContext, safe_text
from .monitor import format_status, monitor
from .platform_windows import configure_startup, startup_shortcut
from .ui_preferences import is_interactive, resolve_ui, select_language
from .wizard import diagnostic, run_wizard

MESSAGES = {
    'title': ('ComfyBridge {version} — 功能选择', 'ComfyBridge {version} — Main menu'),
    'language': ('语言：简体中文', 'Language: English'),
    'current': ('配置：已生效', 'Configuration: current'),
    'pending': ('配置：已保存，待启动或明确重启生效', 'Configuration: saved; start or explicit restart required'),
    'invalid_config': ('配置：缺失或无效；请选择服务设置进行修复', 'Configuration: missing or invalid; open service settings to repair'),
    'unknown': ('后台：身份或运行状态无法确认。请检查旧版窗口及实例状态，不要删除运行文件或按 PID 停进程。', 'Backend: identity/state unconfirmed. Check the old-version window and instance; do not delete runtime files or stop by PID.'),
    'snapshot': ('以下是状态快照；空回车刷新，选项 1 查看实时状态。', 'Status snapshot below; Enter refreshes, option 1 opens live status.'),
    'home_1': ('[1] 查看状态与实时日志', '[1] View live status and logs'),
    'home_2': ('[2] 启动后台', '[2] Start backend'),
    'home_3': ('[3] 停止后台', '[3] Drain and stop backend'),
    'home_4': ('[4] 重启并应用配置', '[4] Restart and apply configuration'),
    'home_5': ('[5] 修改服务连接与 API Key', '[5] Service connection and API key'),
    'home_6': ('[6] 管理模型与缓存', '[6] Models and cache policy'),
    'home_7': ('[7] 管理音色与工作流', '[7] Voices and workflows'),
    'home_8': ('[8] 高级设置', '[8] Advanced settings'),
    'home_9': ('[9] 登录自启动设置', '[9] Login startup settings'),
    'language_entry': ('[A] 语言 / Language', '[A] 语言 / Language'),
    'exit': ('[0] 退出窗口（不停止后台）', '[0] Close window (leave backend running)'),
    'back': ('[0] 返回', '[0] Back'),
    'action': ('请输入选项：', 'Select an option: '),
    'invalid': ('选项无效，请输入显示的编号。', 'Invalid option; enter one of the displayed choices.'),
    'confirm': ('[1] 确认\n[0] 取消（默认）', '[1] Confirm\n[0] Cancel (default)'),
    'continue': ('按回车返回……', 'Press Enter to return...'),
    'started': ('已启动或连接现有后台；是否可合成请查看就绪状态。', 'Backend started or attached; check readiness before synthesis.'),
    'start_failed': ('启动未成功确认。菜单仍可使用；请修复配置或检查其他实例。不会自动反复重试。', 'Startup was not confirmed. This menu remains available; repair settings or check other instances. No automatic retry loop.'),
    'stop_warning': ('停止将先拒绝新请求并等待现有任务结束。确认停止？', 'Stop rejects new requests and waits for existing work. Confirm drain/stop?'),
    'force_warning': ('强制停止只停止 Bridge，远端任务可能继续。账本保留；不清队列、不全局中断。确认？', 'Force stops Bridge only; remote tasks may continue and the journal is retained. No global queue clear/interrupt. Confirm?'),
    'stop_requested': ('已请求停止，后台可能仍在排空；刷新状态查看结果。', 'Stop requested; the backend may still be draining. Refresh status to check completion.'),
    'restart_warning': ('使用已保存配置排空并重启后台；关闭此窗口不会取消重启。确认？', 'Drain and restart using saved configuration; closing this window will not cancel it. Confirm?'),
    'restart_requested': ('独立重启控制进程已启动；这是请求受理，不代表重启已完成。', 'Independent restart controller launched; this acknowledges the request, not completion.'),
    'save_current': ('配置已保存，运行配置已经一致。', 'Saved configuration already matches the running backend.'),
    'apply_restart': ('配置已保存但尚未生效。现在重启应用？选择取消可稍后从主菜单重启。', 'Settings saved but pending. Restart to apply now? Cancel to do it later from the main menu.'),
    'apply_start': ('配置已保存。现在启动后台？', 'Settings saved. Start the backend now?'),
    'setup_cancelled': ('首次配置尚未保存。可从服务设置继续；现在不会启动后台。', 'First setup was not saved. Continue from service settings; no backend will be started now.'),
    'advanced': ('高级设置', 'Advanced settings'),
    'advanced_1': ('[1] 请求、队列、音频及日志限制', '[1] Request, queue, audio and logging limits'),
    'advanced_2': ('[2] 查看脱敏配置摘要', '[2] View redacted configuration summary'),
    'advanced_3': ('[3] 强制停止后台（谨慎）', '[3] Force stop backend (caution)'),
    'advanced_4': ('[4] 核对状态不明的任务（谨慎）', '[4] Resolve uncertain tasks (caution)'),
    'summary': ('配置摘要', 'Configuration summary'),
    'listen': ('监听：{host}:{port}', 'Listen: {host}:{port}'),
    'remote': ('ComfyUI：{url}', 'ComfyUI: {url}'),
    'hidden_key': ('业务 API Key：已设置（隐藏）', 'Business API key: set (hidden)'),
    'data_dir': ('数据目录：{path}', 'Data directory: {path}'),
    'models': ('模型（{count}）：', 'Models ({count}):'),
    'model': ('  {model}  缓存：{policy}', '  {model}  Cache: {policy}'),
    'inherit': ('跟随工作流', 'Follow workflow'),
    'keep': ('保留缓存', 'Keep cached'),
    'release': ('生成后不保留缓存', 'Release after generation'),
    'presets': ('音色路由（{count}）：', 'Voice routes ({count}):'),
    'limits': ('限制（字段名保持协议原名）：', 'Limits (canonical field identifiers):'),
    'logging': ('日志容量：每份 {size} 字节，{count} 份备份', 'Logs: {size} bytes per file, {count} backups'),
    'startup': ('登录自启动设置', 'Login startup settings'),
    'enabled': ('状态：已登记当前数据目录的快捷方式', 'Status: shortcut registered for this data directory'),
    'disabled': ('状态：未登记', 'Status: not registered'),
    'startup_1': ('[1] 启用或更新登录自启动', '[1] Enable or update login startup'),
    'startup_2': ('[2] 禁用登录自启动', '[2] Disable login startup'),
    'startup_warning': ('仅修改当前用户、本数据目录的 Startup 快捷方式。启用后登录时启动后台并打开主菜单；不修改防火墙。确认？', 'Modify only the current-user Startup shortcut for this data directory. Login starts the backend and opens this menu; firewall unchanged. Confirm?'),
    'startup_done': ('快捷方式设置已更新。实际登录行为仍需用户验证；移动程序目录后请重新登记。', 'Shortcut setting updated. Verify actual login behavior; register again after moving the program.'),
    'no_backend': ('没有可确认身份的后台。请先启动，或检查旧版本实例。', 'No authenticated backend is available. Start it or check an old-version instance.'),
    'uncertain': ('状态不明任务：仅人工确认放弃核对，不证明远端已结束，也不会重放请求。', 'Uncertain tasks: acknowledgement abandons reconciliation; it does not prove remote completion and never replays the request.'),
    'no_uncertain': ('当前没有可人工确认的状态不明任务。', 'There are no uncertain tasks eligible for acknowledgement.'),
    'resolve_warning': ('任务 {task_id}\n请先自行核对 ComfyUI。确认放弃核对并承担远端可能仍在运行的风险？', 'Task {task_id}\nInspect ComfyUI yourself first. Acknowledge abandonment while accepting that remote work may still be running?'),
    'resolve_done': ('已记录人工确认；没有提交或重放生成请求。', 'Acknowledgement recorded; no generation was submitted or replayed.'),
}


class Home:
    def __init__(self, data_dir: Path, ui: UIContext, *, ask=None, secret=None, output=None, reader=None):
        self.data_dir = Path(data_dir).resolve()
        self.ui = ui
        self.reader = reader or ConsoleInput()
        self.ask = ask or self.reader.ask
        self.secret = secret or self.reader.secret
        if output is None:
            from rich.console import Console
            console = Console()
            def output(text):
                stripped = text.lstrip()
                style = 'bold cyan' if stripped.startswith('ComfyBridge ') else None
                if stripped.startswith(('[1]', '[2]', '[3]', '[4]', '[5]', '[6]', '[7]', '[8]', '[9]', '[A]', '[S]')):
                    style = 'cyan'
                elif stripped.startswith(('[0]', '---')):
                    style = 'dim'
                console.print(text, style=style, markup=False, highlight=False, soft_wrap=True)
        self.output = output
        self.start_unconfirmed = False

    def t(self, key, **values):
        return self.ui.t(MESSAGES, key, **values)

    def confirm(self, key, **values) -> bool:
        self.output(self.t(key, **values))
        self.output(self.t('confirm'))
        while True:
            answer = self.ask(self.t('action')).strip().lower()
            if answer in ('1', 'y', 'yes'):
                return True
            if answer in ('', '0', 'q', 'n', 'no'):
                return False
            self.output(self.t('invalid'))

    def pause(self):
        self.ask(self.t('continue'))

    def status(self):
        return get_status(self.data_dir)

    def state_unknown(self, status) -> bool:
        return status is None and (self.start_unconfirmed or (self.data_dir / 'runtime/control.json').exists())

    def render(self):
        self.output('\n' + self.t('title', version=__version__))
        self.output('-' * 48)
        self.output(self.t('snapshot'))
        status = self.status()
        from .launcher import _controller_status
        if self.state_unknown(status):
            self.output(self.t('unknown'))
        # Keep the home header compact; detailed routes/tasks belong in Monitor.
        basics = {key: status.get(key) for key in ('ready', 'online', 'draining')} if status else None
        for line in format_status(basics, self.ui)[:3]:
            self.output(line)
        controller = _controller_status(self.data_dir)
        if controller.get('restart', {}).get('state') != 'idle':
            for line in format_status({'backend': None, **controller}, self.ui)[1:]:
                self.output(line)
        try:
            digest = config_digest(load_config(self.data_dir))
            self.output(self.t('current' if status and digest == status.get('config_digest') else 'pending'))
        except (ValueError, OSError):
            self.output(self.t('invalid_config'))
        self.output(self.t('language'))
        for index in range(1, 10):
            if index in (1, 5):
                self.output('')
            self.output(self.t('home_' + str(index)))
        self.output('')
        self.output(self.t('language_entry'))
        self.output(self.t('exit'))
        self.output('-' * 48)

    def start(self):
        from .launcher import ensure_backend
        try:
            ensure_backend(self.data_dir)
            self.start_unconfirmed = False
            self.output(self.t('started'))
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.start_unconfirmed = True
            self.output(self.t('start_failed'))
            self.output(diagnostic(error, self.ui))

    def stop(self, *, force=False):
        from .launcher import request_stop
        if self.confirm('force_warning' if force else 'stop_warning'):
            request_stop(self.data_dir, force=force)
            self.start_unconfirmed = False
            self.output(self.t('stop_requested'))

    def restart(self):
        from .launcher import request_restart
        if self.confirm('restart_warning'):
            request_restart(self.data_dir)
            self.output(self.t('restart_requested'))

    def edit(self, section):
        result = run_wizard(self.data_dir, ui=self.ui, section=section,
                            ask=self.ask, secret=self.secret, output=self.output)
        if result != 0:
            return
        status = self.status()
        if self.state_unknown(status):
            self.output(self.t('unknown'))
            return
        if status is not None:
            if status.get('config_digest') == config_digest(load_config(self.data_dir)):
                self.output(self.t('save_current'))
            elif self.confirm('apply_restart'):
                from .launcher import request_restart
                request_restart(self.data_dir)
                self.output(self.t('restart_requested'))
        elif self.confirm('apply_start'):
            self.start()

    def summary(self):
        config = load_config(self.data_dir)
        self.output(self.t('summary'))
        self.output(self.t('listen', host=safe_text(config.server.listen_host), port=config.server.listen_port))
        self.output(self.t('remote', url=safe_text(config.comfyui.base_url)))
        self.output(self.t('hidden_key'))
        self.output(self.t('data_dir', path=safe_text(str(self.data_dir))))
        self.output(self.t('models', count=len(config.models)))
        for model in config.models:
            self.output(self.t('model', model=safe_text(model.id), policy=self.t(model.cache_policy)))
        self.output(self.t('presets', count=len(config.presets)))
        for preset in config.presets:
            self.output('  ' + safe_text(preset.model) + ' / ' + safe_text(preset.voice))
        self.output(self.t('limits'))
        for field, value in config.limits.model_dump().items():
            self.output(f'  {field}: {value}')
        self.output(self.t('logging', size=config.logging.max_bytes, count=config.logging.backup_count))

    def resolve(self):
        status = self.status()
        if status is None:
            self.output(self.t('no_backend'))
            return
        self.output(self.t('uncertain'))
        rows = []
        for row in status.get('unresolved', []):
            if isinstance(row, dict) and row.get('state') == 'uncertain':
                try:
                    ident = row.get('task_id')
                    if str(uuid.UUID(ident)) == ident:
                        rows.append(row)
                except (ValueError, TypeError, AttributeError):
                    pass
        if not rows:
            self.output(self.t('no_uncertain'))
            return
        for number, row in enumerate(rows, 1):
            self.output(f"[{number}] {row['task_id']}  {safe_text(row.get('error_code') or '')}")
        self.output(self.t('back'))
        while True:
            choice = self.ask(self.t('action')).strip().lower()
            if choice in ('0', 'q', ''):
                return
            if choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(rows):
                task_id = rows[int(choice) - 1]['task_id']
                break
            self.output(self.t('invalid'))
        if self.confirm('resolve_warning', task_id=task_id):
            ControlClient(self.data_dir).command('resolve', {'task_id': task_id, 'acknowledge': True})
            self.output(self.t('resolve_done'))

    def advanced(self):
        while True:
            self.output('\n' + self.t('advanced'))
            for number in range(1, 5):
                self.output(self.t('advanced_' + str(number)))
            self.output(self.t('back'))
            action = self.ask(self.t('action')).strip().lower()
            if action in ('0', 'q'):
                return
            try:
                if action == '1':
                    self.edit('limits')
                elif action == '2':
                    self.summary()
                elif action == '3':
                    self.stop(force=True)
                elif action == '4':
                    self.resolve()
                else:
                    self.output(self.t('invalid'))
                    continue
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.output(diagnostic(error, self.ui))
            self.pause()

    def startup(self):
        while True:
            self.output('\n' + self.t('startup'))
            self.output(self.t('enabled' if startup_shortcut(self.data_dir).is_file() else 'disabled'))
            self.output(self.t('startup_1'))
            self.output(self.t('startup_2'))
            self.output(self.t('back'))
            action = self.ask(self.t('action')).strip().lower()
            if action in ('0', 'q'):
                return
            if action not in ('1', '2'):
                self.output(self.t('invalid'))
                continue
            if self.confirm('startup_warning'):
                configure_startup(self.data_dir, enable=action == '1')
                self.output(self.t('startup_done'))
                self.pause()

    def run(self, *, auto_start=True):
        try:
            if not (self.data_dir / 'config.json').exists():
                result = run_wizard(self.data_dir, ui=self.ui, first_run=True,
                                    ask=self.ask, secret=self.secret, output=self.output)
                if result != 0:
                    self.output(self.t('setup_cancelled'))
                    auto_start = False
            if auto_start and (self.data_dir / 'config.json').exists():
                self.start()  # Exactly once on entry, never on render/return/refresh.
            while True:
                self.render()
                action = self.ask(self.t('action')).strip().lower()
                if action in ('0', 'q'):
                    return 0
                if action == '':
                    continue
                try:
                    if action == '1':
                        monitor(self.data_dir, ui=self.ui, return_to_menu=True, reader=self.reader)
                    elif action == '2':
                        self.start()
                    elif action == '3':
                        self.stop()
                    elif action == '4':
                        self.restart()
                    elif action in ('5', '6', '7'):
                        self.edit({'5': 'server', '6': 'models', '7': 'presets'}[action])
                    elif action == '8':
                        self.advanced()
                    elif action == '9':
                        self.startup()
                    elif action == 'a':
                        select_language(self.data_dir, self.ui, ask=self.ask, output=self.output)
                    else:
                        self.output(self.t('invalid'))
                        continue
                except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                    self.output(diagnostic(error, self.ui))
                if action not in ('1', '8', '9', 'a'):
                    self.pause()
        except (KeyboardInterrupt, EOFError):
            return 0  # Observer only. Never send a lifecycle mutation on exit.


def run_home(data_dir: Path, *, ui=None, interactive=None, ask=None, secret=None, output=None) -> int:
    reader = ConsoleInput()
    ask = ask or reader.ask
    output_for_language = output or print
    try:
        if ui is None:
            ui = resolve_ui(data_dir, interactive=is_interactive() if interactive is None else interactive,
                            ask=ask, output=output_for_language)
        return Home(data_dir, ui, ask=ask, secret=secret, output=output, reader=reader).run()
    except (KeyboardInterrupt, EOFError):
        return 0
