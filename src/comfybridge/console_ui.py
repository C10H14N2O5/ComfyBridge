"""Bilingual home screen: an observer/controller, never a second backend."""
from __future__ import annotations

from pathlib import Path
import uuid

from . import __version__
from .config import load_config
from .console_input import ConsoleInput
from .console_layout import confirm_lines, emit, kv_lines, rich_output, rule, title_block
from .control import ControlClient, config_digest, get_status
from .i18n import UIContext, safe_text
from .monitor import monitor, overview_rows, reason_text
from .platform_windows import AlreadyRunning, InstanceLock, configure_startup, startup_shortcut
from .ui_preferences import is_interactive, resolve_ui, select_language
from .wizard import diagnostic, run_wizard, summary_lines

MESSAGES = {
    'title': ('ComfyBridge {version} — 功能选择', 'ComfyBridge {version} — Main menu'),
    'snapshot': ('状态快照（空回车刷新，选项 1 查看实时状态）', 'Status snapshot (Enter refreshes; option 1 opens live status)'),
    'config_label': ('配置', 'Configuration'),
    'current': ('已生效', 'current'),
    'pending': ('已保存，待启动或明确重启生效', 'saved; start or explicit restart required'),
    'invalid_config': ('缺失或无效；请选择服务设置进行修复', 'missing or invalid; open service settings to repair'),
    'language_label': ('语言', 'Language'),
    'language_value': ('简体中文', 'English'),
    'unknown': ('后台身份或运行状态无法确认\n'
                '  - 请检查旧版窗口及实例状态\n'
                '  - 不要删除运行文件，也不要按 PID 停止进程',
                'Backend identity or state unconfirmed\n'
                '  - Check the old-version window and instance\n'
                '  - Do not delete runtime files or stop processes by PID'),
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
    'confirm_yes': ('[1] 确认', '[1] Confirm'),
    'confirm_no': ('[0] 取消（默认）', '[0] Cancel (default)'),
    'continue': ('按回车返回……', 'Press Enter to return...'),
    'started': ('已启动或连接现有后台；是否可合成请查看就绪状态。', 'Backend started or attached; check readiness before synthesis.'),
    'start_failed': ('启动未成功确认\n'
                     '  - 菜单仍可使用；请修复配置或检查其他实例\n'
                     '  - 不会自动反复重试',
                     'Startup was not confirmed\n'
                     '  - This menu remains available; repair settings or check other instances\n'
                     '  - No automatic retry loop'),
    'stop_warning': ('排空并停止后台？\n'
                     '  - 先拒绝新请求，再等待现有任务结束',
                     'Drain and stop the backend?\n'
                     '  - New requests are rejected; existing work finishes first'),
    'force_warning': ('强制停止后台（仅 Bridge）？\n'
                      '  - 远端任务可能继续\n'
                      '  - 任务账本保留\n'
                      '  - 不清空队列，也不全局中断',
                      'Force stop the backend (Bridge only)?\n'
                      '  - Remote tasks may continue\n'
                      '  - The journal is retained\n'
                      '  - No global queue clear or interrupt'),
    'stop_requested': ('已请求停止，后台可能仍在排空；刷新状态查看结果。', 'Stop requested; the backend may still be draining. Refresh status to check completion.'),
    'restart_warning': ('使用已保存配置排空并重启后台？\n'
                        '  - 关闭此窗口不会取消重启',
                        'Drain and restart using the saved configuration?\n'
                        '  - Closing this window will not cancel it'),
    'restart_requested': ('独立重启控制进程已启动\n'
                          '  - 这表示请求已受理，不代表重启已完成',
                          'Independent restart controller launched\n'
                          '  - This acknowledges the request, not completion'),
    'save_current': ('配置已保存，运行配置已经一致。', 'Saved configuration already matches the running backend.'),
    'apply_restart': ('配置已保存，但尚未生效。现在重启应用？\n'
                      '  - 选择取消可稍后从主菜单重启',
                      'Settings saved but not yet applied. Restart to apply now?\n'
                      '  - Cancel to do it later from the main menu'),
    'apply_start': ('配置已保存。现在启动后台？', 'Settings saved. Start the backend now?'),
    'setup_cancelled': ('首次配置尚未保存\n'
                        '  - 可从服务设置继续\n'
                        '  - 现在不会启动后台',
                        'First setup was not saved\n'
                        '  - Continue from service settings\n'
                        '  - No backend will be started now'),
    'advanced': ('高级设置', 'Advanced settings'),
    'advanced_1': ('[1] 请求、队列、音频及日志限制', '[1] Request, queue, audio and logging limits'),
    'advanced_2': ('[2] 查看脱敏配置摘要', '[2] View redacted configuration summary'),
    'advanced_3': ('[3] 强制停止后台（谨慎）', '[3] Force stop backend (caution)'),
    'advanced_4': ('[4] 核对状态不明的任务（谨慎）', '[4] Resolve uncertain tasks (caution)'),
    'advanced_5': ('[5] 清理闲置工作流文件', '[5] Clean up unused workflow files'),
    'cleanup_title': ('清理闲置工作流文件', 'Clean up unused workflow files'),
    'cleanup_count': ('未被引用的文件', 'Unreferenced files'),
    'cleanup_size': ('合计大小', 'Total size'),
    'cleanup_none': ('没有未被当前配置引用的工作流文件。', 'No workflow files are unreferenced by the current configuration.'),
    'cleanup_config_invalid': ('配置缺失或无效，无法判断哪些模板在用；未删除任何文件。',
                               'Configuration missing or invalid; cannot tell which templates are in use. No files were deleted.'),
    'cleanup_locked': ('配置编辑器或其他操作正在使用此数据目录，请关闭后重试；未删除任何文件。',
                       'The configuration editor or another operation is using this data directory; close it and retry. No files were deleted.'),
    'cleanup_warning': ('永久删除以上 {count} 个未被当前已保存配置引用的工作流文件？\n'
                        '  - 当前配置引用的模板不会删除\n'
                        '  - 运行中的后台不受影响（已加载，且启动快照有副本）\n'
                        '  - 删除后无法恢复；config.json.bak 引用的模板不受保护',
                        'Permanently delete the {count} workflow file(s) above, unreferenced by the saved configuration?\n'
                        '  - Templates referenced by the current configuration are kept\n'
                        '  - A running backend is unaffected (already loaded; its snapshot has copies)\n'
                        '  - This cannot be undone; templates referenced only by config.json.bak are not protected'),
    'advanced_6': ('[6] 清理运行时临时文件', '[6] Clean up runtime temporary files'),
    'runtime_title': ('清理运行时临时文件', 'Clean up runtime temporary files'),
    'runtime_snapshots': ('旧启动快照', 'Old startup snapshots'),
    'runtime_temporary': ('残留临时文件', 'Leftover temporary files'),
    'runtime_none': ('没有可清理的运行时临时文件。', 'There are no runtime temporary files to clean up.'),
    'runtime_unknown': ('后台正在运行，但无法确认其使用的快照；未删除任何文件。',
                        'A backend is running but its snapshot cannot be identified; no files were deleted.'),
    'runtime_locked': ('后台启动、重启或配置编辑正在进行，请完成后重试；未删除任何文件。',
                       'A backend start, restart or configuration edit is in progress; retry when it finishes. No files were deleted.'),
    'runtime_warning': ('永久删除以上 {count} 项运行时临时文件？\n'
                        '  - 运行中、上次成功启动及重启记录所用的快照会保留\n'
                        '  - 任务账本、状态文件和锁文件不受影响\n'
                        '  - 删除后无法恢复',
                        'Permanently delete the {count} runtime item(s) above?\n'
                        '  - Snapshots of the running backend, last successful start and recorded restart are kept\n'
                        '  - The task journal, state files and lock files are unaffected\n'
                        '  - This cannot be undone'),
    'runtime_done': ('已删除 {count} 项运行时临时文件。', 'Deleted {count} runtime item(s).'),
    'cleanup_done': ('已删除 {count} 个闲置工作流文件。', 'Deleted {count} unused workflow file(s).'),
    'cleanup_failed': ('{count} 个文件未能删除，请检查文件占用或权限。', '{count} file(s) could not be deleted; check file locks or permissions.'),
    'summary_title': ('配置摘要', 'Configuration summary'),
    'data_dir': ('数据目录', 'Data directory'),
    'startup': ('登录自启动设置', 'Login startup settings'),
    'startup_status': ('状态', 'Status'),
    'enabled': ('已登记当前数据目录的快捷方式', 'shortcut registered for this data directory'),
    'disabled': ('未登记', 'not registered'),
    'startup_1': ('[1] 启用或更新登录自启动', '[1] Enable or update login startup'),
    'startup_2': ('[2] 禁用登录自启动', '[2] Disable login startup'),
    'startup_warning': ('修改登录自启动？\n'
                        '  - 仅修改当前用户、本数据目录的 Startup 快捷方式\n'
                        '  - 启用后登录时启动后台并打开主菜单\n'
                        '  - 不修改防火墙',
                        'Change login startup?\n'
                        '  - Modifies only the current-user Startup shortcut for this data directory\n'
                        '  - Login starts the backend and opens this menu\n'
                        '  - Firewall unchanged'),
    'startup_done': ('快捷方式设置已更新\n'
                     '  - 实际登录行为仍需用户验证\n'
                     '  - 移动程序目录后请重新登记',
                     'Shortcut setting updated\n'
                     '  - Verify actual login behavior yourself\n'
                     '  - Register again after moving the program'),
    'no_backend': ('没有可确认身份的后台。请先启动，或检查旧版本实例。', 'No authenticated backend is available. Start it or check an old-version instance.'),
    'uncertain': ('状态不明的任务\n'
                  '  - 人工确认只是放弃核对，不证明远端已结束\n'
                  '  - 不会重放请求',
                  'Uncertain tasks\n'
                  '  - Acknowledgement abandons reconciliation; it does not prove remote completion\n'
                  '  - The request is never replayed'),
    'no_uncertain': ('当前没有可人工确认的状态不明任务。', 'There are no uncertain tasks eligible for acknowledgement.'),
    'resolve_warning': ('确认放弃核对任务 {task_id}？\n'
                        '  - 请先自行核对 ComfyUI\n'
                        '  - 远端可能仍在运行，风险由你承担',
                        'Acknowledge abandoning reconciliation for task {task_id}?\n'
                        '  - Inspect ComfyUI yourself first\n'
                        '  - Remote work may still be running; you accept this risk'),
    'resolve_done': ('已记录人工确认；没有提交或重放生成请求。', 'Acknowledgement recorded; no generation was submitted or replayed.'),
}


class Home:
    def __init__(self, data_dir: Path, ui: UIContext, *, ask=None, secret=None, output=None, reader=None):
        self.data_dir = Path(data_dir).resolve()
        self.ui = ui
        self.reader = reader or ConsoleInput()
        self.ask = ask or self.reader.ask
        self.secret = secret or self.reader.secret
        self.output = output or rich_output()
        self.start_unconfirmed = False

    def t(self, key, **values):
        return self.ui.t(MESSAGES, key, **values)

    def lines(self, lines):
        for line in lines:
            self.output(line)

    def say(self, key, **values):
        emit(self.output, self.t(key, **values))

    def result(self, key, **values):
        """Action feedback: blank line, then the message at column 0."""
        self.output('')
        self.say(key, **values)

    def show_error(self, error):
        self.output('')
        emit(self.output, diagnostic(error, self.ui))

    def confirm(self, key, **values) -> bool:
        self.lines(confirm_lines(self.t(key, **values), self.t('confirm_yes'), self.t('confirm_no')))
        while True:
            answer = self.ask(self.t('action')).strip().lower()
            if answer in ('1', 'y', 'yes'):
                return True
            if answer in ('', '0', 'q', 'n', 'no'):
                return False
            self.output(self.t('invalid'))

    def pause(self):
        self.output('')
        self.ask(self.t('continue'))

    def status(self):
        return get_status(self.data_dir)

    def state_unknown(self, status) -> bool:
        return status is None and (self.start_unconfirmed or (self.data_dir / 'runtime/control.json').exists())

    def render(self):
        self.lines(title_block(self.t('title', version=__version__)))
        self.say('snapshot')
        status = self.status()
        from .launcher import _controller_status
        if self.state_unknown(status):
            self.output('')
            self.say('unknown')
        # Keep the home header compact; detailed routes/tasks belong in Monitor.
        basics = {key: status.get(key) for key in ('ready', 'online', 'draining')} if status else None
        state = {'backend': basics}
        controller = _controller_status(self.data_dir)
        if controller.get('restart', {}).get('state') != 'idle':
            state.update(controller)
        rows = overview_rows(state, self.ui)
        try:
            digest = config_digest(load_config(self.data_dir))
            config_value = self.t('current' if status and digest == status.get('config_digest') else 'pending')
        except (ValueError, OSError):
            config_value = self.t('invalid_config')
        rows.append((self.t('config_label'), config_value))
        rows.append((self.t('language_label'), self.t('language_value')))
        self.output('')
        self.lines(kv_lines(rows))
        for index in range(1, 10):
            if index in (1, 5):
                self.output('')
            self.output(self.t('home_' + str(index)))
        self.output('')
        self.output(self.t('language_entry'))
        self.output(self.t('exit'))
        self.output(rule())

    def start(self):
        from .launcher import ensure_backend
        try:
            ensure_backend(self.data_dir)
            self.start_unconfirmed = False
            self.result('started')
        except (OSError, ValueError, RuntimeError, TimeoutError) as error:
            self.start_unconfirmed = True
            self.result('start_failed')
            emit(self.output, diagnostic(error, self.ui))

    def stop(self, *, force=False):
        from .launcher import request_stop
        if self.confirm('force_warning' if force else 'stop_warning'):
            request_stop(self.data_dir, force=force)
            self.start_unconfirmed = False
            self.result('stop_requested')

    def restart(self):
        from .launcher import request_restart
        if self.confirm('restart_warning'):
            request_restart(self.data_dir)
            self.result('restart_requested')

    def edit(self, section):
        result = run_wizard(self.data_dir, ui=self.ui, section=section,
                            ask=self.ask, secret=self.secret, output=self.output)
        if result != 0:
            return
        status = self.status()
        if self.state_unknown(status):
            self.output('')
            self.say('unknown')
            return
        if status is not None:
            if status.get('config_digest') == config_digest(load_config(self.data_dir)):
                self.result('save_current')
            elif self.confirm('apply_restart'):
                from .launcher import request_restart
                request_restart(self.data_dir)
                self.result('restart_requested')
        elif self.confirm('apply_start'):
            self.start()

    def summary(self):
        config = load_config(self.data_dir)
        self.lines(title_block(self.t('summary_title')))
        self.lines(kv_lines([(self.t('data_dir'), safe_text(str(self.data_dir)))]))
        # Shared wizard renderer: redacted key, localized limit names, no cleanup text.
        self.lines(summary_lines(config.model_dump(mode='json'), self.ui))

    def resolve(self):
        status = self.status()
        if status is None:
            self.result('no_backend')
            return
        self.output('')
        self.say('uncertain')
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
            self.result('no_uncertain')
            return
        self.output('')
        for number, row in enumerate(rows, 1):
            self.output(f"[{number}] {row['task_id']}")
            if row.get('error_code'):
                self.output(reason_text(row.get('error_code'), self.ui))
        self.output('')
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
            self.result('resolve_done')

    def cleanup_workflows(self):
        """List, confirm, then delete templates the saved configuration no longer uses."""
        from .workflows import remove_unused_templates, unused_templates
        try:
            candidates = unused_templates(self.data_dir, load_config(self.data_dir))
        except (ValueError, OSError):
            self.result('cleanup_config_invalid')
            return
        if not candidates:
            self.result('cleanup_none')
            return
        size = 0
        for relative in candidates:
            try:
                size += (self.data_dir / relative).stat().st_size
            except OSError:
                pass
        self.lines(title_block(self.t('cleanup_title')))
        self.lines(kv_lines([(self.t('cleanup_count'), str(len(candidates))),
                             (self.t('cleanup_size'), f'{size / 1024:.1f} KiB')]))
        self.output('')
        for relative in candidates:
            self.output('  ' + safe_text(relative.split('/', 1)[1]))
        self.output('')
        if not self.confirm('cleanup_warning', count=len(candidates)):
            return
        try:
            # No lock while waiting for the user; recheck against the latest saved config.
            with InstanceLock(self.data_dir, 'config'):
                try:
                    config = load_config(self.data_dir)
                except (ValueError, OSError):
                    self.result('cleanup_config_invalid')
                    return
                removed, failed = remove_unused_templates(self.data_dir, candidates, config)
        except AlreadyRunning:
            self.result('cleanup_locked')
            return
        self.result('cleanup_done', count=len(removed))
        if failed:
            self.say('cleanup_failed', count=len(failed))

    def cleanup_runtime(self):
        """List, confirm, then delete old snapshots and stale private temporary files."""
        from . import launcher
        try:
            plan = launcher.runtime_cleanup_plan(self.data_dir,
                                                 backend_running=launcher.backend_lock_held(self.data_dir))
        except ValueError:
            self.result('runtime_unknown')
            return
        if not plan.count:
            self.result('runtime_none')
            return
        self.lines(title_block(self.t('runtime_title')))
        self.lines(kv_lines([(self.t('runtime_snapshots'), str(len(plan.snapshots))),
                             (self.t('runtime_temporary'), str(len(plan.temporary))),
                             (self.t('cleanup_size'), f'{plan.size / 1024:.1f} KiB')]))
        self.output('')
        for name in plan.snapshots:
            self.output('  snapshots/' + safe_text(name))
        for name in plan.temporary:
            self.output('  ' + safe_text(name))
        self.output('')
        if not self.confirm('runtime_warning', count=plan.count):
            return
        try:
            removed, failed = launcher.clean_runtime(self.data_dir, plan)
        except AlreadyRunning:
            self.result('runtime_locked')
            return
        except ValueError:
            self.result('runtime_unknown')
            return
        self.result('runtime_done', count=len(removed))
        if failed:
            self.say('cleanup_failed', count=len(failed))

    def advanced(self):
        while True:
            self.lines(title_block(self.t('advanced')))
            for number in range(1, 7):
                self.output(self.t('advanced_' + str(number)))
            self.output('')
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
                elif action == '5':
                    self.cleanup_workflows()
                elif action == '6':
                    self.cleanup_runtime()
                else:
                    self.output(self.t('invalid'))
                    continue
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                self.show_error(error)
            self.pause()

    def startup(self):
        while True:
            self.lines(title_block(self.t('startup')))
            registered = startup_shortcut(self.data_dir).is_file()
            self.lines(kv_lines([(self.t('startup_status'), self.t('enabled' if registered else 'disabled'))]))
            self.output('')
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
                self.result('startup_done')
                self.pause()

    def run(self, *, auto_start=True):
        try:
            if not (self.data_dir / 'config.json').exists():
                result = run_wizard(self.data_dir, ui=self.ui, first_run=True,
                                    ask=self.ask, secret=self.secret, output=self.output)
                if result != 0:
                    self.result('setup_cancelled')
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
                    self.show_error(error)
                if action not in ('1', '8', '9', 'a'):
                    self.pause()
        except (KeyboardInterrupt, EOFError):
            return 0  # Observer only. Never send a lifecycle mutation on exit.


def run_home(data_dir: Path, *, ui=None, interactive=None, ask=None, secret=None, output=None) -> int:
    reader = ConsoleInput()
    ask = ask or reader.ask
    output_for_language = output or rich_output()
    try:
        if ui is None:
            ui = resolve_ui(data_dir, interactive=is_interactive() if interactive is None else interactive,
                            ask=ask, output=output_for_language)
        return Home(data_dir, ui, ask=ask, secret=secret, output=output, reader=reader).run()
    except (KeyboardInterrupt, EOFError):
        return 0
