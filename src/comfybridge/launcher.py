"""Detached backend, explicit restart controller and CLI entry point."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import uuid

from .console_layout import confirm_lines, plain_output
from .control import ControlClient, ControlError, config_digest, get_status, new_identity, publish_identity, remove_identity, create_control_app
from .platform_windows import AlreadyRunning, InstanceLock, atomic_private_json, private_runtime, protect_path, spawn_detached


class LocalStartFailure(RuntimeError):
    """Child exited before publishing a validated control identity."""


def default_data_dir() -> Path:
    root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
    return root / 'data'


def _snapshot_path(data_dir: Path, snapshot: str) -> Path:
    if str(uuid.UUID(snapshot)) != snapshot:
        raise ValueError('invalid_snapshot')
    runtime = private_runtime(data_dir)
    target = runtime / 'snapshots' / snapshot
    if not target.resolve().is_relative_to(runtime.resolve()):
        raise ValueError('snapshot_outside_runtime')
    return target


def capture_snapshot(data_dir: Path) -> str:
    """Under config lock, freeze config and immutable templates, without generation."""
    from .config import load_config, save_config
    from .workflows import load_presets
    with InstanceLock(data_dir, 'config'):
        config = load_config(data_dir)
        load_presets(config, data_dir)
        snapshot = str(uuid.uuid4())
        target = _snapshot_path(data_dir, snapshot)
        target.mkdir(parents=True)
        protect_path(target)
        for preset in config.presets:
            source = (data_dir / preset.template).resolve()
            destination = target / preset.template
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        save_config(target, config)
        load_presets(config, target)
    return snapshot


def snapshot_config(data_dir: Path, snapshot: str):
    from .config import load_config
    from .workflows import load_presets
    root = _snapshot_path(data_dir, snapshot)
    config = load_config(root)
    load_presets(config, root)
    return config


def restore_snapshot_templates(data_dir: Path, snapshot: str) -> None:
    """Restore the exact verified immutable JSONs; leave pending config untouched."""
    config = snapshot_config(data_dir, snapshot)
    root = _snapshot_path(data_dir, snapshot)
    with InstanceLock(data_dir, 'config'):
        workflows = (data_dir / 'workflows').resolve()
        if not workflows.is_relative_to(data_dir.resolve()):
            raise ValueError('template_directory_outside_data')
        for preset in config.presets:
            destination = (data_dir / preset.template).resolve()
            if not destination.is_relative_to(workflows):
                raise ValueError('template_outside_workflows')
            destination.parent.mkdir(parents=True, exist_ok=True)
            # atomic replacement avoids partial templates if interrupted
            content = (root / preset.template).read_bytes()
            temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(content)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)


STALE_TEMP_SECONDS = 600


@dataclass(frozen=True)
class RuntimeCleanup:
    """Runtime items eligible for deletion; nothing here is ever a live state file."""
    snapshots: tuple[str, ...] = ()
    temporary: tuple[str, ...] = ()
    size: int = 0

    @property
    def count(self) -> int:
        return len(self.snapshots) + len(self.temporary)


def _canonical_uuid(value) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _snapshot_references(path: Path, keys: tuple[str, ...]) -> set[str]:
    try:
        if _is_link(path) or path.stat().st_size > 16384:
            return set()
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {raw[key] for key in keys if _canonical_uuid(raw.get(key))}


def protected_snapshots(data_dir: Path, *, backend_running: bool) -> set[str]:
    """Snapshots used by a running backend, last-good start or recorded restart.

    A running backend whose snapshot cannot be identified makes every snapshot
    potentially live: refuse rather than guess.
    """
    runtime = Path(data_dir) / 'runtime'
    running = _snapshot_references(runtime / 'control.json', ('snapshot',))
    if backend_running and not running:
        raise ValueError('runtime_backend_snapshot_unknown')
    return (running | _snapshot_references(runtime / 'last-good.json', ('snapshot',))
            | _snapshot_references(runtime / 'restart.json', ('snapshot', 'fallback')))


def _tree_size(path: Path) -> int:
    total = 0
    for directory, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def runtime_cleanup_plan(data_dir: Path, *, backend_running: bool, now: float | None = None) -> RuntimeCleanup:
    """List unreferenced snapshot directories and stale private temporary files.

    Only canonical-UUID snapshot directories and ``.private-*`` files older than
    STALE_TEMP_SECONDS directly in ``runtime`` are eligible; links are never listed.
    """
    runtime = Path(data_dir) / 'runtime'
    if _is_link(runtime) or not runtime.is_dir():
        return RuntimeCleanup()
    protected = protected_snapshots(data_dir, backend_running=backend_running)
    now = time.time() if now is None else now
    snapshots, temporary, size = [], [], 0
    root = runtime / 'snapshots'
    if not _is_link(root) and root.is_dir():
        for entry in sorted(root.iterdir()):
            if (_canonical_uuid(entry.name) and entry.name not in protected
                    and not _is_link(entry) and entry.is_dir()):
                snapshots.append(entry.name)
                size += _tree_size(entry)
    for entry in sorted(runtime.iterdir()):
        if not entry.name.startswith('.private-') or _is_link(entry) or not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if now - stat.st_mtime >= STALE_TEMP_SECONDS:
            temporary.append(entry.name)
            size += stat.st_size
    return RuntimeCleanup(tuple(snapshots), tuple(temporary), size)


def backend_lock_held(data_dir: Path) -> bool:
    """Whether a backend currently owns this data directory (a momentary probe)."""
    try:
        with InstanceLock(Path(data_dir), 'backend'):
            return False
    except AlreadyRunning:
        return True


def clean_runtime(data_dir: Path, plan: RuntimeCleanup) -> tuple[list[str], list[str]]:
    """Delete the confirmed plan, rechecked under the start/config/backend locks.

    Holding ``start`` excludes ensure_backend and the restart controller for the
    whole operation; ``config`` excludes snapshot capture and the editor. Raises
    AlreadyRunning when either is busy. Returns ``(removed, failed)`` labels.
    """
    data_dir = Path(data_dir)
    with ExitStack() as stack:
        stack.enter_context(InstanceLock(data_dir, 'start'))
        stack.enter_context(InstanceLock(data_dir, 'config'))
        try:
            stack.enter_context(InstanceLock(data_dir, 'backend'))
            backend_running = False
        except AlreadyRunning:
            backend_running = True
        current = runtime_cleanup_plan(data_dir, backend_running=backend_running)
        runtime = (data_dir / 'runtime').resolve()
        removed, failed = [], []
        for name in sorted(set(plan.snapshots) & set(current.snapshots)):
            path = data_dir / 'runtime' / 'snapshots' / name
            label = 'snapshots/' + name
            try:
                if _is_link(path) or path.resolve().parent != runtime / 'snapshots':
                    continue
                shutil.rmtree(path)  # does not follow links or junctions
                removed.append(label)
            except OSError:
                failed.append(label)
        for name in sorted(set(plan.temporary) & set(current.temporary)):
            path = data_dir / 'runtime' / name
            try:
                if _is_link(path) or path.resolve().parent != runtime:
                    continue
                path.unlink(missing_ok=True)
                removed.append(name)
            except OSError:
                failed.append(name)
        return removed, failed


def _controller_status(data_dir: Path) -> dict:
    try:
        path = data_dir / 'runtime/restart.json'
        if path.stat().st_size > 4096:
            return {'restart': {'state': 'invalid'}}
        raw = json.loads(path.read_text(encoding='utf-8'))
        return {'restart': {k: raw[k] for k in ('state', 'error', 'snapshot', 'fallback') if k in raw}}
    except (OSError, ValueError, TypeError):
        return {'restart': {'state': 'idle'}}


def _set_restart(data_dir: Path, state: str, **fields) -> None:
    atomic_private_json(private_runtime(data_dir) / 'restart.json', {'state': state, **fields})


async def serve(data_dir: Path, snapshot: str | None = None) -> int:
    import uvicorn
    from .api import create_app
    from .service import BridgeService
    from .logging_utils import configure_logging
    with InstanceLock(data_dir, 'backend'):
        snapshot = snapshot or capture_snapshot(data_dir)
        config = snapshot_config(data_dir, snapshot)
        restore_snapshot_templates(data_dir, snapshot)
        configure_logging(data_dir, config.logging)
        service = BridgeService(config, data_dir)
        sockets = []
        tasks = []
        identity = None
        started = False
        stop_requested = False
        force_requested = False
        def shutdown(force: bool):
            nonlocal stop_requested, force_requested
            stop_requested = True
            force_requested = force_requested or force
        try:
            # Bind both sockets first: a foreign business port is never queried/killed.
            for host, port in ((config.server.listen_host, config.server.listen_port), ('127.0.0.1', 0)):
                family = socket.AF_INET6 if ':' in host else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                sockets.append(sock)
                if os.name == 'nt':
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((host, port))
                sock.listen(128)
                sock.setblocking(False)
            identity = new_identity(data_dir, sockets[1].getsockname()[1], config, snapshot)
            admin = create_control_app(service, identity, shutdown,
                status_extra=lambda: {**_controller_status(data_dir), 'stop_requested': stop_requested, 'force_requested': force_requested,
                    'running_config': {'listen_host': config.server.listen_host, 'listen_port': config.server.listen_port,
                                       'comfyui_url': str(config.comfyui.base_url), 'models': [m.id for m in config.models],
                                       'presets': len(config.presets), 'api_key': '[redacted]'}})
            await service.start()
            started = True
            servers = [uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan='off',
                       timeout_graceful_shutdown=2)) for app in (create_app(config, data_dir, service=service), admin)]
            tasks = [asyncio.create_task(server.serve(sockets=[sock])) for server, sock in zip(servers, sockets)]
            while not all(server.started for server in servers):
                if any(task.done() for task in tasks):
                    raise LocalStartFailure('listener_start_failed')
                await asyncio.sleep(.02)
            atomic_private_json(private_runtime(data_dir) / 'last-good.json', {'snapshot': snapshot, 'config_digest': config_digest(config)})
            publish_identity(data_dir, identity)
            logging.getLogger(__name__).info('backend_started')
            while not any(task.done() for task in tasks):
                if stop_requested and (force_requested or service.is_drained()):
                    break
                await asyncio.sleep(.1)
            # Force closes owned tasks only; no operating-system PID kill is ever used.
            for server in servers:
                server.should_exit = True
            if force_requested:
                await service.close(force=True)
                started = False
            await asyncio.gather(*tasks, return_exceptions=True)
            return 0
        finally:
            if started:
                await service.close(force=force_requested)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            for sock in sockets:
                sock.close()
            if identity:
                remove_identity(data_dir, identity['instance_id'])


def _wait_start(data_dir: Path, child, snapshot: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_status(data_dir)
        if status:
            if status.get('snapshot') != snapshot:
                raise ControlError('unexpected_backend_instance')
            return status
        if child.poll() is not None:
            raise LocalStartFailure('backend_local_start_failed')
        time.sleep(.1)
    # Do not rollback a merely slow startup, which might still own sockets/mutex.
    raise TimeoutError('backend_start_unconfirmed_no_fallback')


def _start_snapshot(data_dir: Path, snapshot: str, timeout: float = 20) -> dict:
    child = spawn_detached(['serve', '--data-dir', str(data_dir.resolve()), '--snapshot', snapshot])
    return _wait_start(data_dir, child, snapshot, timeout)


def ensure_backend(data_dir: Path, timeout: float = 20) -> dict:
    current = get_status(data_dir)
    if current:
        return current
    try:
        with InstanceLock(data_dir, 'start'):
            current = get_status(data_dir)
            if current:
                return current
            # Fail closed on stale identity while a backend mutex remains held.
            with InstanceLock(data_dir, 'backend'):
                snapshot = capture_snapshot(data_dir)
            return _start_snapshot(data_dir, snapshot, timeout)
    except AlreadyRunning:
        # Another launch/controller can be between spawning and publication. Attach,
        # never compete by starting a second backend or guessing from a business port.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = get_status(data_dir)
            if current:
                return current
            time.sleep(.1)
        raise ControlError('backend_launch_or_control_unconfirmed') from None


def request_stop(data_dir: Path, *, force: bool = False) -> dict:
    # User stop cancels a waiting controller; stopping must not restart us.
    # The controller's own graceful stop uses ControlClient directly instead.
    atomic_private_json(private_runtime(data_dir) / 'restart-cancel.json', {'cancel': True, 'force': force})
    return ControlClient(data_dir).command('stop', {'force': force})


def request_restart(data_dir: Path):
    # Validation before detaching; the controller validates/captures again under lock.
    from .config import load_config
    from .workflows import load_presets
    load_presets(load_config(data_dir), data_dir)
    with InstanceLock(data_dir, 'controller'):
        (private_runtime(data_dir) / 'restart-cancel.json').unlink(missing_ok=True)
    return spawn_detached(['_restart-controller', '--data-dir', str(data_dir.resolve())])


def _cancel_started_backend(data_dir: Path, cancel: Path) -> bool:
    if not cancel.exists():
        return False
    # A user stop can arrive between old backend exit and new identity publication.
    # Honor it now, using nonce authentication rather than killing a guessed PID.
    intent = json.loads(cancel.read_text(encoding='utf-8'))
    ControlClient(data_dir).command('stop', {'force': intent.get('force') is True})
    _set_restart(data_dir, 'cancelled')
    return True


def restart_controller(data_dir: Path, *, poll_interval: float = .25) -> int:
    """Independent transient process. One fallback ONLY on proven local exit."""
    with InstanceLock(data_dir, 'controller'), InstanceLock(data_dir, 'start'):
        cancel = private_runtime(data_dir) / 'restart-cancel.json'
        candidate = capture_snapshot(data_dir)
        old = get_status(data_dir)
        previous = old.get('snapshot') if old else None
        _set_restart(data_dir, 'draining', snapshot=candidate)
        try:
            if old:
                client = ControlClient(data_dir)
                client.command('drain')
                while True:
                    if cancel.exists():
                        _set_restart(data_dir, 'cancelled')
                        return 1
                    status = client.status()
                    if status.get('force_requested'):
                        _set_restart(data_dir, 'cancelled')
                        return 1
                    if status.get('drained'):
                        break
                    time.sleep(poll_interval)
                client.command('stop', {'force': False})
                # Wait for lifetime mutex, NOT disappearance of a PID or HTTP port.
                while True:
                    if cancel.exists():
                        _set_restart(data_dir, 'cancelled')
                        return 1
                    try:
                        with InstanceLock(data_dir, 'backend'):
                            break
                    except AlreadyRunning:
                        time.sleep(poll_interval)
            if cancel.exists():
                _set_restart(data_dir, 'cancelled')
                return 1
            _set_restart(data_dir, 'starting', snapshot=candidate)
            try:
                _start_snapshot(data_dir, candidate)
            except LocalStartFailure:
                if cancel.exists():
                    _set_restart(data_dir, 'cancelled')
                    return 1
                if not previous or previous == candidate:
                    raise
                snapshot_config(data_dir, previous)
                _set_restart(data_dir, 'fallback_starting', snapshot=candidate, fallback=previous, error='pending_config_start_failed')
                _start_snapshot(data_dir, previous)  # exactly one fallback, no recursive retry
                if _cancel_started_backend(data_dir, cancel):
                    return 1
                _set_restart(data_dir, 'fallback_running', snapshot=candidate, fallback=previous, error='pending_config_start_failed')
                return 1
            if _cancel_started_backend(data_dir, cancel):
                return 1
            _set_restart(data_dir, 'completed', snapshot=candidate)
            return 0
        except (OSError, ValueError, RuntimeError, TimeoutError):
            _set_restart(data_dir, 'failed', snapshot=candidate, error='restart_failed_check_local_config_and_logs')
            return 1


def parse_arguments(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description='ComfyBridge local backend and monitor')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir())
    parser.add_argument('--installation-self-test', action='store_true', help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest='command')
    for name in ('serve', 'start', 'monitor', 'configure', 'status', 'stop', 'restart', '_restart-controller', 'resolve', 'startup'):
        command = commands.add_parser(name)
        command.add_argument('--data-dir', type=Path, default=argparse.SUPPRESS)
        if name == 'serve':
            command.add_argument('--snapshot')
        if name == 'stop':
            command.add_argument('--force', action='store_true')
        if name in ('stop', 'resolve', 'startup'):
            command.add_argument('--yes', action='store_true', help='explicitly confirm this administrative operation')
        if name == 'resolve':
            command.add_argument('task_id')
        if name == 'startup':
            command.add_argument('action', choices=['enable', 'disable'])
    return parser.parse_args(argv)


MESSAGES = {
    'confirm': ('确认管理操作？', 'Confirm administrative operation?'),
    'confirm_yes': ('[1] 确认', '[1] Confirm'),
    'confirm_no': ('[0] 取消（默认）', '[0] Cancel (default)'),
    'choice_prompt': ('请输入选项：', 'Select an option: '),
    'force': ('仅强制停止 Bridge？\n  - 远端任务可能继续\n  - 任务账本保留',
              'Force stop Bridge only?\n  - Remote tasks may continue\n  - The journal is retained'),
    'restart': ('独立重启控制进程已启动\n  - 可以关闭管理窗口\n  - 不代表重启已经完成',
                'Independent restart controller started\n  - The monitor may be closed\n  - Restart is not yet confirmed complete'),
    'local_failure': ('后台本地启动失败\n  - 检查端口冲突和音频工具\n  - 可从主菜单的服务设置修复配置',
                      'Backend local startup failed\n  - Check port conflicts and audio tools\n  - Repair settings from the main menu'),
    'locked': ('本数据目录有其他操作正在进行\n  - 请检查状态或旧版窗口\n  - 不要删除运行文件',
               'Another operation owns this data directory\n  - Check status or the old-version window\n  - Do not delete runtime files'),
    'identity': ('无法确认匹配的后台身份\n  - 请检查状态，或先用旧版窗口停止旧后台\n  - 勿按 PID 停止进程',
                 'No matching authenticated backend identity\n  - Check status or stop the old backend using its old-version window\n  - Never stop by PID alone'),
}


def _configure_stdio() -> None:
    # Real Windows consoles already use Unicode console I/O. Redirected output
    # has a stable UTF-8 contract, independent of the machine's ANSI code page.
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None and not stream.isatty() and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='backslashreplace')
        except (OSError, ValueError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = parse_arguments(argv)
    if args.installation_self_test:
        from .install_platform import run_installation_self_test
        return run_installation_self_test()
    if not getattr(sys, 'frozen', False):
        return _run_command(args, args.data_dir.resolve())
    from .install_platform import SharedLifetimeLeases, InstallPlatformError
    try:
        with SharedLifetimeLeases(Path(sys.executable).parent, args.data_dir) as leases:
            return _run_command(args, leases.data)
    except InstallPlatformError as error:
        # Do not inspect business config/control state to guess whether Setup is safe.
        output = plain_output(sys.stderr)
        output('安装或数据目录当前不可用，请完成安装恢复或手动关闭相关程序后重试。')
        output('Installation/data unavailable; finish Setup recovery or manually close related programs and retry.')
        output('错误代码 / Error code: ' + error.code)
        return 1


def _run_command(args, data_dir: Path) -> int:
    ui = None
    try:
        # Internal/headless protocols must never prompt for language or start a UI.
        if args.command == 'serve':
            return asyncio.run(serve(data_dir, args.snapshot))
        if args.command == '_restart-controller':
            return restart_controller(data_dir)
        if args.command == 'status':
            status = get_status(data_dir)
            print(json.dumps(status or {'state': 'not_running', **_controller_status(data_dir)}, ensure_ascii=False, indent=2))
            return 0 if status else 1
        if args.command is None:
            from .console_ui import run_home
            return run_home(data_dir)
        from .ui_preferences import resolve_ui, is_interactive
        from .console_input import ConsoleInput
        reader = ConsoleInput()
        human_ui = args.command in ('configure', 'monitor')
        ui = resolve_ui(data_dir, interactive=human_ui and is_interactive(), ask=reader.ask,
                        output=plain_output() if human_ui else lambda _: None)
        if args.command == 'configure':
            from .wizard import run_wizard
            return run_wizard(data_dir, ui=ui, first_run=not (data_dir / 'config.json').exists(),
                              ask=reader.ask, secret=reader.secret)
        if args.command in ('stop', 'resolve', 'startup'):
            key = 'force' if args.command == 'stop' and args.force else 'confirm'
            if not args.yes:
                output = plain_output()
                for line in confirm_lines(ui.t(MESSAGES, key), ui.t(MESSAGES, 'confirm_yes'), ui.t(MESSAGES, 'confirm_no')):
                    output(line)
                if reader.ask(ui.t(MESSAGES, 'choice_prompt')).strip().lower() not in ('1', 'y', 'yes'):
                    return 1
            if args.command == 'stop':
                request_stop(data_dir, force=args.force)
            elif args.command == 'resolve':
                ControlClient(data_dir).command('resolve', {'task_id': args.task_id, 'acknowledge': True})
            else:
                from .platform_windows import configure_startup
                configure_startup(data_dir, enable=args.action == 'enable')
            return 0
        if args.command == 'restart':
            request_restart(data_dir)
            plain_output()('\n' + ui.t(MESSAGES, 'restart'))
            return 0
        if args.command == 'monitor':
            from .monitor import monitor
            return monitor(data_dir, ui=ui, reader=reader)
        if not (data_dir / 'config.json').exists():
            if not is_interactive():
                raise FileNotFoundError('configuration_required')
            from .wizard import run_wizard
            if run_wizard(data_dir, ui=ui, first_run=True, ask=reader.ask, secret=reader.secret) != 0:
                return 1
        ensure_backend(data_dir)
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        # Pydantic and upstream errors can contain secrets; never print raw exceptions.
        from .i18n import UIContext
        from .wizard import diagnostic
        ui = ui or UIContext()
        output = plain_output(sys.stderr)
        output('')
        if isinstance(error, LocalStartFailure):
            output(ui.t(MESSAGES, 'local_failure'))
        elif isinstance(error, AlreadyRunning):
            output(ui.t(MESSAGES, 'locked'))
        elif isinstance(error, ControlError):
            output(ui.t(MESSAGES, 'identity'))
        else:
            output(diagnostic(error, ui))
        return 1
    except (KeyboardInterrupt, EOFError):
        return 0
