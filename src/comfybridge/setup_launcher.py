"""Offline Setup observer: fixed executable-root target, explicit safe decisions.

This UI never starts/stops the business backend, chooses language preferences, or
performs installation file operations itself. Installer owns all mutations.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Callable

from .console_input import ConsoleInput
from .console_layout import confirm_lines, emit, kv_lines, plain_output, section, title_block
from .i18n import UIContext, safe_text


MESSAGES = {
    'title': ('ComfyBridge 离线安装程序', 'ComfyBridge Offline Setup'),
    'language_title': ('选择界面语言', 'Choose interface language'),
    'language_exit': ('[0] 退出 / Exit', '[0] 退出 / Exit'),
    'language_prompt': ('请选择 / Select: ', '请选择 / Select: '),
    'language_invalid': ('请选择 1、2 或 0；语言没有默认值。', 'Choose 1, 2 or 0; there is no default language.'),
    'plan_title': ('安装计划', 'Installation plan'),
    'notice_title': ('继续前请注意', 'Before proceeding'),
    'scope_title': ('备份范围', 'Backup scope'),
    'modified_title': ('本地修改的受管理文件', 'Locally modified managed files'),
    'label_target': ('安装位置', 'Installation location'),
    'label_old_version': ('已有版本', 'Existing version'),
    'label_new_version': ('新版本', 'New version'),
    'label_data': ('此次备份数据目录', 'Data directory for this backup'),
    'label_backup': ('此次事务备份', 'This transaction backup'),
    'label_code': ('错误代码', 'Error code'),
    'cancel_plain': ('取消', 'Cancel'),
    'install': ('安装', 'Install'),
    'update': ('更新', 'Update'),
    'exit': ('退出', 'Exit'),
    'select': ('请选择编号', 'Select a number'),
    'invalid': ('无效选项，请选择显示的编号', 'Invalid choice; select a displayed number'),
    'target': ('安装位置：{path}', 'Installation location: {path}'),
    'old_version': ('已有版本：{version}', 'Existing version: {version}'),
    'new_version': ('新版本：{version}', 'New version: {version}'),
    'not_installed': ('未安装', 'not installed'),
    'unknown': ('未知', 'unknown'),
    'root_notice': ('  - 安装位置固定为 Setup 可执行文件所在目录\n  - 不会使用当前工作目录或临时解包目录', '  - The target is the Setup executable directory\n  - Not the current working directory or temporary extraction directory'),
    'stop_notice': ('  - 继续前请手动停止 ComfyBridge 后台\n  - 关闭全部 Monitor、主菜单及相关程序窗口\n  - Setup 不会替您停止任何进程', '  - Before proceeding, manually stop the ComfyBridge backend\n  - Close every Monitor, home-menu and related program window\n  - Setup never stops processes for you'),
    'fresh_notice': ('首次安装目录必须为空，仅可保留当前 Setup 文件', 'A fresh installation directory must be empty except for this Setup file'),
    'data_menu': ('请选择此次备份的数据目录', 'Choose the data directory to back up for this transaction'),
    'data_default': ('相邻 data 目录（默认；回车选择）', 'Adjacent data directory (default; Enter selects)'),
    'data_external': ('另选外部绝对路径（仅用于备份）', 'Choose an external absolute path (backup only)'),
    'cancel': ('取消（默认）', 'Cancel (default)'),
    'external_notice': ('外部目录只决定此次备份范围\n  - 不会修改配置、启动参数或 --data-dir\n  - 原有启动方式保持不变', 'An external directory changes only this backup scope\n  - It does not change configuration, launch arguments or --data-dir\n  - Existing launch behavior is unchanged'),
    'external_path': ('请输入已有数据目录的绝对路径（0 取消）', 'Enter the absolute path of the existing data directory (0 cancels)'),
    'absolute_required': ('必须输入不含控制字符的绝对路径；不会自动改为其他目录', 'An absolute path without control characters is required; no directory is substituted automatically'),
    'data_path': ('此次备份数据目录：{path}', 'Data directory backed up in this transaction: {path}'),
    'backup_path': ('此次事务备份：{path}', 'This transaction backup: {path}'),
    'backup_none': ('此次没有可删除的备份', 'There is no backup to delete for this transaction'),
    'backup_scope': ('  - 备份原安装目录的完整内容及所选数据目录\n  - 不包含 Setup 本身、安装事务状态和以前的备份\n  - 未选择的外部目录不在此次备份范围内', '  - Includes the full prior installation directory and the selected data directory\n  - Excludes Setup itself, installer transaction state and earlier backups\n  - Unselected external directories are outside this backup'),
    'modified': ('以下受管理文件有本地修改；更新前会备份，替换或保留规则见提示：', 'The following managed files have local modifications; they are backed up before update and the replacement/retention notices apply:'),
    'modified_none': ('没有检测到受管理文件的本地修改', 'No local modifications of managed files were detected'),
    'more': ('另有 {count} 项未显示', '{count} more entries are not shown'),
    'same_version': ('新旧版本相同：此次是修复，会重新写入受管理文件；不是普通升级', 'The versions are identical: this is a repair that rewrites managed files, not a normal upgrade'),
    'confirm_install': ('确认在以上位置安装？', 'Install in the location shown above?'),
    'confirm_update': ('确认按以上范围备份并更新？', 'Back up and update with the scope shown above?'),
    'confirm_repair': ('已了解同版本修复会重写受管理文件，确认修复？', 'Repair this same version, acknowledging that managed files will be rewritten?'),
    'confirm': ('确认', 'Confirm'),
    'cancelled': ('已取消，未发起新的安装或更新', 'Cancelled; no new installation/update was started'),
    'busy': ('检测到程序仍在运行，或无法安全确认已关闭\n  - 请手动停止后台并关闭所有界面，再重新检查\n  - 不会强制结束进程', 'A program is still running, or safe closure cannot be confirmed\n  - Please manually stop the backend and close all interfaces, then recheck\n  - No process will be forcibly ended'),
    'recheck': ('重新检查', 'Recheck'),
    'recovering': ('正在检查上次中断的安装事务', 'Checking for an interrupted installation transaction'),
    'recovered': ('已恢复上次中断的安装事务；原有事务备份已保留', 'Recovered the interrupted installation transaction; its backup was retained'),
    'recovery_none': ('没有需要恢复的安装事务', 'No installation transaction requires recovery'),
    'recovery_rolled_back': ('已回滚中断的事务，恢复原有文件；备份已保留', 'The interrupted transaction was rolled back and original files restored; the backup was retained'),
    'recovery_committed': ('已核对完成的安装事务并完成恢复检查；备份已保留', 'The completed installation transaction was checked and recovery finalized; the backup was retained'),
    'starting': ('正在执行已确认的本地安装事务，请勿关闭此窗口', 'Executing the confirmed local installation transaction; do not close this window'),
    'success': ('文件安装及本地自检已通过\n  - 这不代表已验证真实 TTS 业务或 GPU 生成', 'File installation and local self-test passed\n  - This does not verify real TTS business requests or GPU generation'),
    'no_autostart': ('后台未自动启动；可稍后手动打开 ComfyBridge', 'The backend was not automatically started; open ComfyBridge manually when ready'),
    'delete_question': ('是否删除刚完成的这一次事务备份？\n  - 只会删除下方所示备份\n  - 不会清理其他备份', 'Delete only the backup from this completed transaction?\n  - Only the backup shown below is affected\n  - Other backups are not cleared'),
    'delete_yes': ('是，仅删除此次备份', 'Yes, delete only this backup'),
    'delete_no': ('否，保留备份（默认）', 'No, keep the backup (default)'),
    'backup_kept': ('此次备份已保留', 'This backup was retained'),
    'backup_deleted': ('此次备份已删除，其他备份未改动', 'This backup was deleted; other backups were not changed'),
    'backup_delete_failed': ('安装已成功，但无法确认此次备份已删除。请保留并检查，不会尝试清理其他备份', 'Installation succeeded, but deletion of this backup was not confirmed. Keep/check it; no other backups will be cleared'),
    'pause': ('按回车关闭此窗口', 'Press Enter to close this window'),
    'interrupted': ('操作已中断；若事务已经开始，请再次打开同一 Setup 检查恢复。备份不会自动删除', 'Operation interrupted; if a transaction started, reopen this Setup for recovery. Backups are not automatically deleted'),
    'source_context': ('源码运行需由测试代码显式提供隔离上下文；正常使用请运行打包后的 Setup', 'Source execution requires an explicitly injected isolated test context; normal use requires packaged Setup'),
    'bundle_missing': ('找不到可信的嵌入式安装包，请重新获取完整 Setup', 'The embedded installation bundle is unavailable; obtain a complete Setup'),
    'arguments': ('Setup 不接受命令行安装位置或安装动作参数；请使用交互菜单', 'Setup accepts no command-line install target or operation arguments; use the interactive menu'),
    'help': ('把 Setup 放到目标目录后运行；安装位置固定为 Setup 所在目录', 'Place Setup in the target directory and run it; installation targets its own directory'),
    'error_generic': ('安装操作未完成。请检查完整安装包、目录权限及程序关闭状态；错误详情已省略以避免泄露私密数据', 'Installation operation did not complete. Check the complete bundle, directory permissions and program closure; private error details are omitted'),
    'error_downgrade': ('不允许降级。请使用相同或更新版本的 Setup', 'Downgrades are not allowed. Use a same-version or newer Setup'),
    'error_not_empty': ('首次安装目录不是空目录；请把 Setup 放到空目录（仅保留 Setup）', 'The fresh installation directory is not empty; place Setup in an empty directory containing only Setup'),
    'error_product': ('现有目录无法确认为受支持的 ComfyBridge 安装；不会覆盖不明程序', 'The directory cannot be identified as a supported ComfyBridge installation; unknown programs will not be overwritten'),
    'error_payload': ('安装包或清单无效、损坏或校验不通过；未授权使用不可信文件', 'The payload/manifest is invalid, damaged or fails verification; untrusted files are not authorized'),
    'error_path': ('安装、备份或数据路径不安全或不受支持；请检查目录、链接与磁盘格式', 'An install, backup or data path is unsafe/unsupported; check directories, links and filesystem format'),
    'error_selftest': ('本地自检未通过。请查看恢复结果并保留此次备份；不要把这视为安装成功', 'Local self-test failed. Check recovery results and retain this backup; do not treat this as a successful installation'),
    'error_recovery': ('恢复操作未完成。请保留所有事务文件和备份，暂勿运行程序或继续更新', 'Recovery did not complete. Retain all transaction files/backups; do not run the application or continue updating yet'),
    'error_changed': ('目录内容在确认后发生变化；请重新检查并重新确认', 'Directory contents changed after confirmation; inspect and confirm again'),
    'error_code': ('错误代码：{code}', 'Error code: {code}'),
    'error_disk_space': ('安装或备份磁盘空间不足。请在下方位置所在磁盘释放空间后重试；不会自动清理旧备份', 'The install/backup volume lacks free space. Free space on the volume shown below and retry; old backups are not automatically cleared'),
    'error_collision': ('新包路径与现有未知文件或冲突路径重叠，已拒绝覆盖。请先检查下方目录并手动整理冲突文件', 'A package path collides with an existing unknown file or conflicting path; overwrite was refused. Inspect the directory below and resolve the collision manually'),
    'error_backup_collision': ('此次备份位置已存在或冲突。请保留现有备份并重新检查，不会覆盖旧备份', 'This backup destination already exists or conflicts. Preserve existing backups and recheck; old backups will not be overwritten'),
    'error_data_missing': ('所选外部数据目录不存在或不可读取，请重新选择正确的已有目录', 'The selected external data directory is missing or unreadable; select the correct existing directory'),
    'error_acl': ('无法为安装状态或备份设置当前用户专用权限。请使用受支持且权限正常的本地目录', 'Private current-user permissions could not be applied to installer state/backups. Use a supported local directory with working permissions'),
    'error_verify': ('文件或备份校验失败。请保留事务文件及备份，检查磁盘和完整安装包后再重试', 'File or backup verification failed. Retain transaction files/backups and check the disk and complete bundle before retrying'),
    'error_io': ('本地文件读写失败，请检查下方目录的权限、磁盘状态及占用情况；不会强制结束进程', 'Local file I/O failed. Check permissions, disk health and file usage in the directories below; no process will be forcibly ended'),
    'error_mode': ('当前目录不适合所选操作：新安装请使用空目录，已有安装请选择更新或明确的同版本修复', 'The selected operation does not match this directory: install into an empty directory, or use update/explicit same-version repair for an existing installation'),
    'error_scope_limit': ('目录文件数量超过安全检查上限。请检查安装及数据目录范围，避免选择无关的大型目录', 'The directory file count exceeds the safety-check limit. Review install/data scope and avoid unrelated large directories'),
    'error_operation': ('安装事务未完成，核心已尝试恢复。请保留全部备份，再次打开 Setup 核对恢复结果后重试', 'The transaction did not complete and the core attempted recovery. Retain all backups and reopen Setup to check recovery before retrying'),
    'warning_unknown': ('核心报告了额外注意事项，请重新检查安装范围；未经识别的文本未显示', 'The core reported an additional caution; review the installation scope. Unrecognized text was omitted'),
    'legacy_inventory_untrusted': ('旧版缺少可信安装清单；只会按安装核心的兼容规则识别程序文件', 'The legacy installation lacks a trusted inventory; program files are identified only by the core compatibility rules'),
    'legacy_retired_preserved': ('无法可信确认的旧版退役文件将保留，不会自动删除', 'Legacy retired files that cannot be identified reliably are preserved, not automatically deleted'),
    'preserved_modified_retired': ('已被本地修改的退役程序文件将保留', 'Retired program files with local modifications are preserved'),
    'stage_verify': ('正在校验安装文件', 'Verifying installation files'),
    'stage_commit': ('正在提交安装事务', 'Committing the installation transaction'),
    'stage_cleanup': ('正在清理临时事务文件，保留事务备份', 'Cleaning temporary transaction files while retaining the backup'),
    'stage_backup': ('正在备份', 'Backing up'),
    'stage_extract': ('正在校验并准备安装文件', 'Verifying and staging installation files'),
    'stage_install': ('正在替换受管理文件', 'Replacing managed files'),
    'stage_selftest': ('正在运行本地自检（非真实 TTS）', 'Running local self-test (not real TTS)'),
    'stage_rollback': ('正在恢复原有文件', 'Restoring original files'),
}

_BUSY_CODES = frozenset({'installer_busy', 'install_busy', 'install_process_uninspectable',
                         'install_resource_uninspectable', 'installer_process_uninspectable',
                         'installer_resource_uninspectable'})
_ERROR_KEYS = {
    'setup_source_context': 'source_context', 'setup_bundle_missing': 'bundle_missing',
    'setup_arguments': 'arguments', 'installer_downgrade': 'error_downgrade',
    'installer_not_empty': 'error_not_empty', 'installer_root_not_empty': 'error_not_empty',
    'installer_product_invalid': 'error_product', 'installer_unknown_installation': 'error_product',
    'installer_payload_invalid': 'error_payload', 'installer_manifest_invalid': 'error_payload',
    'installer_path_unsafe': 'error_path', 'install_path_unsafe': 'error_path',
    'install_volume_unsupported': 'error_path', 'installer_selftest_failed': 'error_selftest',
    'installer_rollback_failed': 'error_recovery', 'installer_recovery_failed': 'error_recovery',
    'install_transaction_incomplete': 'error_recovery', 'installer_plan_stale': 'error_changed',
    'installer_install_not_empty': 'error_not_empty', 'installer_disk_space': 'error_disk_space',
    'installer_unknown_collision': 'error_collision', 'installer_path_collision': 'error_collision',
    'installer_backup_collision': 'error_backup_collision', 'installer_data_missing': 'error_data_missing',
    'installer_acl_failed': 'error_acl', 'installer_io_error': 'error_io',
    'installer_backup_verify_failed': 'error_verify', 'installer_installed_verify_failed': 'error_verify',
    'installer_source_changed': 'error_changed', 'installer_plan_invalid': 'error_changed',
    'installer_operation_failed': 'error_operation', 'installer_mode_invalid': 'error_mode',
    'installer_recovery_required': 'error_recovery', 'installer_transaction_incomplete': 'error_recovery',
    'installer_rollback_conflict': 'error_recovery', 'installer_rollback_verify_failed': 'error_recovery',
    'installer_state_invalid': 'error_recovery', 'installer_receipt_invalid': 'error_product',
    'installer_backup_identity': 'error_backup_collision', 'installer_path_invalid': 'error_path',
    'installer_reserved_path': 'error_path', 'installer_volume_unsupported': 'error_path',
    'installer_version_invalid': 'error_payload', 'installer_payload_collision': 'error_payload',
    'installer_payload_link': 'error_payload', 'installer_archive_digest': 'error_payload',
    'installer_payload_digest': 'error_payload', 'installer_private_acl_failed': 'error_acl',
    'installer_lease_unavailable': 'error_io', 'installer_self_test_failed': 'error_selftest',
    'installer_tree_too_large': 'error_scope_limit',
}
_VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?\Z', re.ASCII)


class SetupUIError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class _Cancelled(Exception):
    pass


@dataclass(frozen=True)
class SetupContext:
    root: Path
    payload_zip: Path
    payload_manifest: Path
    setup_path: Path

    @classmethod
    def packaged(cls) -> 'SetupContext':
        if not getattr(sys, 'frozen', False):
            raise SetupUIError('setup_source_context')
        bundle_value = getattr(sys, '_MEIPASS', None)
        if not isinstance(bundle_value, (str, Path)):
            raise SetupUIError('setup_bundle_missing')
        from .install_platform import assert_safe_path
        executable_input, bundle_input = Path(sys.executable), Path(bundle_value)
        if not executable_input.is_absolute() or not bundle_input.is_absolute():
            raise SetupUIError('installer_path_invalid')
        # Validate lexical ancestors BEFORE resolve can erase a junction/symlink.
        executable = assert_safe_path(executable_input, allow_missing=False)
        bundle = assert_safe_path(bundle_input, allow_missing=False)
        payload = assert_safe_path(bundle / 'setup-payload.zip')
        manifest = assert_safe_path(bundle / 'setup-manifest.json')
        if not executable.is_file() or not payload.is_file() or not manifest.is_file():
            raise SetupUIError('setup_bundle_missing')
        return cls(executable.parent, payload, manifest, executable)


def _core_factory(context: SetupContext):
    from .installer_core import Installer
    return Installer(context.root, context.payload_zip, context.payload_manifest, setup_path=context.setup_path)


def _code(error: BaseException) -> str | None:
    value = getattr(error, 'code', None)
    return value if isinstance(value, str) else None


def _bilingual(key: str, **values) -> list[str]:
    return [UIContext(language).t(MESSAGES, key, **values) for language in ('zh-CN', 'en-US')]


class SetupConsole:
    def __init__(self, context: SetupContext, installer, *, reader=None, output=None):
        self.context = context
        self.installer = installer
        self.reader = reader or ConsoleInput()
        self.output = output or plain_output()  # no Rich markup interpretation
        self.ui = UIContext('en-US')
        self._progress_state: tuple[str, int, int] | None = None
        self._selected_data: Path | None = None
        self._last_plan = None

    def t(self, key: str, **values) -> str:
        return self.ui.t(MESSAGES, key, **values)

    def lines(self, lines):
        for line in lines:
            self.output(line)

    def say(self, key: str, **values):
        emit(self.output, self.t(key, **values))

    def result(self, key: str):
        self.output('')
        self.say(key)

    def option(self, number: str, key: str):
        self.output(f'[{number}] {self.t(key)}')

    def choose_language(self) -> bool:
        # Session-only, before ANY inspection or recovery. Never access ui.json.
        self.lines(title_block(' / '.join(_bilingual('title'))))
        self.lines(_bilingual('language_title'))
        self.output('[1] 简体中文')
        self.output('[2] English')
        self.output(self.t('language_exit'))
        while True:
            answer = self.reader.ask(self.t('language_prompt')).strip().lower()
            if answer in ('0', 'q'):
                return False
            if answer in ('1', '2'):
                self.ui.set_language('zh-CN' if answer == '1' else 'en-US')
                return True
            self.lines(_bilingual('language_invalid'))

    def ask(self, key: str = 'select') -> str:
        punctuation = '：' if self.ui.language == 'zh-CN' else ': '
        return self.reader.ask(self.t(key) + punctuation).strip()

    def path_row(self, key: str, value):
        return self.t(key), safe_text(value, max_length=1024) if isinstance(value, (str, Path)) else self.t('unknown')

    def pause(self):
        try:
            self.ask('pause')
        except (EOFError, KeyboardInterrupt):
            pass

    def error(self, error: BaseException):
        code = _code(error)
        self.result(_ERROR_KEYS.get(code, 'error_generic'))
        rows = [(self.t('label_code'), code)] if code in _ERROR_KEYS else []
        # Only approved context/plan paths, never arbitrary exception metadata.
        rows.append(self.path_row('label_target', self.context.root))
        if self._selected_data is not None:
            rows.append(self.path_row('label_data', self._selected_data))
        if self._last_plan is not None and self._last_plan.backup_dir is not None:
            rows.append(self.path_row('label_backup', self._last_plan.backup_dir))
        self.lines(kv_lines(rows))

    def confirm(self, key: str) -> bool:
        self.lines(confirm_lines(self.t(key), '[1] ' + self.t('confirm'), '[0] ' + self.t('cancel')))
        while True:
            try:
                answer = self.ask().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            if answer in ('1', 'y', 'yes'):
                return True
            if answer in ('', '0', 'n', 'no', 'q'):
                return False
            self.say('invalid')

    def with_recheck(self, action: Callable):
        while True:
            try:
                return action()
            except Exception as error:
                if _code(error) not in _BUSY_CODES:
                    raise
                self.lines(confirm_lines(self.t('busy'), '[1] ' + self.t('recheck'), '[0] ' + self.t('cancel')))
                while True:
                    try:
                        answer = self.ask().lower()
                    except (EOFError, KeyboardInterrupt):
                        raise _Cancelled() from None
                    if answer in ('', '0', 'q', 'n'):
                        raise _Cancelled() from None
                    if answer == '1':
                        break
                    self.say('invalid')

    def version_value(self, value) -> str:
        if value is None:
            return self.t('not_installed')
        if not isinstance(value, str) or len(value) > 80 or not _VERSION.fullmatch(value):
            return self.t('unknown')
        return value

    def show_path(self, key: str, value):
        labels = {'target': 'label_target', 'data_path': 'label_data', 'backup_path': 'label_backup'}
        self.lines(kv_lines([self.path_row(labels[key], value)]))

    def select_data(self) -> Path:
        self.lines(section(self.t('data_menu')))
        self.option('1', 'data_default')
        self.option('2', 'data_external')
        self.option('0', 'cancel_plain')
        while True:
            answer = self.ask().lower()
            if answer in ('', '1'):
                return self.context.root / 'data'
            if answer in ('0', 'q'):
                raise _Cancelled()
            if answer == '2':
                self.say('external_notice')
                while True:
                    raw = self.ask('external_path').strip('"')
                    if raw.lower() in ('0', 'q'):
                        raise _Cancelled()
                    if raw and safe_text(raw, max_length=len(raw)) == raw and Path(raw).is_absolute():
                        return Path(raw)
                    self.say('absolute_required')
            self.say('invalid')

    def show_plan(self, plan):
        self._last_plan = plan
        self.lines(title_block(self.t('plan_title')))
        self.lines(kv_lines([
            self.path_row('label_target', plan.root),
            (self.t('label_old_version'), self.version_value(plan.old_version)),
            (self.t('label_new_version'), self.version_value(plan.new_version)),
            self.path_row('label_data', plan.data_dir),
            self.path_row('label_backup', plan.backup_dir),
        ]))
        self.lines(section(self.t('scope_title')))
        self.say('backup_scope')
        modified = plan.managed_modified
        if modified:
            self.lines(section(self.t('modified_title')))
            self.say('modified')
            for path in modified[:100]:
                self.output('  ' + (safe_text(path, max_length=1024) if isinstance(path, (str, Path)) else self.t('unknown')))
            if len(modified) > 100:
                self.say('more', count=len(modified) - 100)
        else:
            self.say('modified_none')
        for warning in plan.warnings:
            if warning in ('same_version', 'repair', 'installer_same_version'):
                continue  # explicit repair warning is rendered separately below
            if warning in ('external_data', 'external_data_backup_only'):
                self.say('external_notice')
            elif warning in ('managed_modified', 'modified_managed_files', 'modified_managed'):
                continue  # the exact bounded list is already visible
            elif warning in ('legacy_inventory_untrusted', 'legacy_retired_preserved', 'preserved_modified_retired'):
                self.say(warning)
            else:
                self.say('warning_unknown')
        self.lines(section(self.t('notice_title')))
        self.say('stop_notice')
        if plan.mode == 'repair':
            self.say('same_version')

    def progress(self, stage: str, completed: int, total: int):
        # No filesystem path, exception, manifest content or arbitrary stage text
        # is passed through to the terminal.
        stages = {'backup': 'stage_backup', 'extract': 'stage_extract', 'stage': 'stage_extract',
                  'install': 'stage_install', 'replace': 'stage_install', 'apply': 'stage_install',
                  'selftest': 'stage_selftest', 'self_test': 'stage_selftest', 'validate': 'stage_verify',
                  'verify': 'stage_verify', 'commit': 'stage_commit', 'cleanup': 'stage_cleanup',
                  'rollback': 'stage_rollback'}
        key = stages.get(stage) if isinstance(stage, str) else None
        if (key is None or type(completed) is not int or type(total) is not int
                or not 0 <= completed <= total <= 1_000_000_000):
            return
        bucket = completed * 10 // total if total else 10
        state = (key, bucket, total)
        if state == self._progress_state:
            return  # at most stage changes and roughly ten percent boundaries
        self._progress_state = state
        self.output(f'  {self.t(key)} [{completed}/{total}]')

    def backup_decision(self, result):
        if result.backup_dir is None:
            self.say('backup_none')
            return
        self.output('')
        self.say('delete_question')
        self.show_path('backup_path', result.backup_dir)
        self.option('1', 'delete_yes')
        self.option('0', 'delete_no')
        while True:
            try:
                answer = self.ask().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ''
            if answer in ('', '0', 'n', 'no', 'q'):
                self.say('backup_kept')
                return
            if answer in ('1', 'y', 'yes'):
                try:
                    # Use only the exact result of this successful execute call.
                    # Recovered/older backups are never offered to delete.
                    self.installer.delete_backup(result)
                except Exception:
                    self.say('backup_delete_failed')
                else:
                    self.say('backup_deleted')
                return
            self.say('invalid')

    def run(self) -> int:
        try:
            if not self.choose_language():
                return 0
        except (EOFError, KeyboardInterrupt):
            self.lines(_bilingual('interrupted'))
            return 1
        self.lines(title_block(self.t('title')))
        self.show_path('target', self.context.root)
        self.say('root_notice')
        self.lines(section(self.t('notice_title')))
        self.say('stop_notice')
        try:
            self.output('')
            self.say('recovering')
            recovery = self.with_recheck(self.installer.recover)
            outcome = getattr(recovery, 'outcome', None)
            outcome_keys = {'rolled_back': 'recovery_rolled_back', 'rollback': 'recovery_rolled_back',
                            'committed': 'recovery_committed', 'completed': 'recovery_committed'}
            key = outcome_keys.get(outcome, 'recovered') if isinstance(outcome, str) else 'recovered'
            self.say(key if recovery.recovered else 'recovery_none')
            if recovery.recovered and recovery.backup_dir is not None:
                self.show_path('backup_path', recovery.backup_dir)
            inspection = self.with_recheck(self.installer.inspect)
            self.output('')
            self.lines(kv_lines([
                (self.t('label_old_version'), self.version_value(inspection.installed_version)),
                (self.t('label_new_version'), self.version_value(inspection.target_version)),
            ]))
            while True:
                self.output('')
                self.option('1', 'install')
                self.option('2', 'update')
                self.option('0', 'exit')
                answer = self.ask().lower()
                if answer in ('0', 'q'):
                    return 0
                if answer not in ('1', '2'):
                    self.say('invalid')
                    continue
                try:
                    inspection = self.with_recheck(self.installer.inspect)
                    if answer == '1':
                        self.say('fresh_notice')
                        mode, data_dir = 'install', self.context.root / 'data'
                    else:
                        mode = 'repair' if (inspection.installed_version == inspection.target_version
                                            and 'repair' in inspection.modes) else 'update'
                        data_dir = self.select_data()
                    self._selected_data = data_dir
                    self._last_plan = None
                    plan = self.with_recheck(lambda: self.installer.plan(mode, data_dir=data_dir))
                    self.show_plan(plan)
                    key = 'confirm_repair' if plan.mode == 'repair' else ('confirm_install' if plan.mode == 'install' else 'confirm_update')
                    if not self.confirm(key):
                        self.say('cancelled')
                        continue
                    self.result('starting')
                    result = self.with_recheck(lambda: self.installer.execute(plan, progress=self.progress))
                    if result.selftest_passed is not True:
                        raise SetupUIError('installer_selftest_failed')
                    self.result('success')
                    self.say('no_autostart')
                    self.backup_decision(result)
                    self.pause()
                    return 0
                except _Cancelled:
                    self.say('cancelled')
                except (EOFError, KeyboardInterrupt):
                    raise
                except Exception as error:
                    self.error(error)
                    # A failed transactional operation may require recovery, not
                    # an immediate second attempt. Exit and retain every backup.
                    self.pause()
                    return 1
        except _Cancelled:
            self.say('cancelled')
            self.pause()
            return 1
        except (EOFError, KeyboardInterrupt):
            self.say('interrupted')
            return 1
        except Exception as error:
            self.error(error)
            self.pause()
            return 1


def _configure_stdio() -> None:
    """Use UTF-8 for redirected output without changing an interactive console."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            if stream.isatty():
                continue
            reconfigure = getattr(stream, 'reconfigure', None)
            if callable(reconfigure):
                reconfigure(encoding='utf-8', errors='replace', line_buffering=True)
        except (AttributeError, OSError, TypeError, ValueError):
            # Capture streams and detached/closed handles may not support this.
            # Never import the business launcher or start a backend as a fallback.
            pass


def main(argv: list[str] | None = None, *, context: SetupContext | None = None,
         installer_factory: Callable | None = None, reader=None, output=None) -> int:
    """No target-selection CLI. Explicit dependency injection is for source tests."""
    _configure_stdio()
    reader = reader or ConsoleInput()
    output = output or plain_output()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (['--help'], ['-h']):
        for text in _bilingual('help'):
            emit(output, text)
        return 0
    try:
        if arguments:
            raise SetupUIError('setup_arguments')
        context = context or SetupContext.packaged()
        installer = (installer_factory or _core_factory)(context)
    except Exception as error:
        key = _ERROR_KEYS.get(_code(error), 'error_generic')
        for text in _bilingual(key):
            emit(output, text)
        code = _code(error)
        if code in _ERROR_KEYS:
            for text in _bilingual('error_code', code=code):
                output(text)
        if context is not None:
            for text in _bilingual('target', path=context.root):
                output(text)
        for text in _bilingual('pause'):
            output(text)
        try:
            reader.ask('请选择 / Select: ')
        except (EOFError, KeyboardInterrupt):
            pass
        return 2 if arguments else 1
    return SetupConsole(context, installer, reader=reader, output=output).run()
