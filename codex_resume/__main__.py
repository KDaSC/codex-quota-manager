import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .app import AppError, Desktop, check_version, open_selected_thread
from .controller import Controller
from .policy import decide, quota_status
from .rpc import ReadOnlyServer
from .runtime import codex_binary, default_app_path, platform_name, process_exists
from .store import Store
from .tasks import inspect_task, list_conversations, stored_assessment


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError('需要准确的任务 UUID') from exc


def positive(value):
    n = int(value)
    if not 1 <= n <= 100:
        raise argparse.ArgumentTypeError('次数应在 1 到 100 之间')
    return n


def parser():
    p = argparse.ArgumentParser(description='Codex App 本地额度恢复续跑工具')
    p.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    p.add_argument('--app', type=Path, default=default_app_path(),
                   help='macOS App 包路径，或 Windows Codex App 的 codex.exe 路径')
    p.add_argument('--state-dir', type=Path, default=Path.home() / '.codex-auto-resume')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor', help='只读检查 App 版本、连接和真实额度')
    sub.add_parser('list', help='分页读取所有可用本地对话，包含归档；不会自动托管')
    sub.add_parser('status', help='查看监控记录')
    web = sub.add_parser('web', help='启动本机浏览器控制台')
    web.add_argument('--port', type=int, default=8765)
    check = sub.add_parser('check', help='只读识别所选对话的任务状态与托管资格')
    check.add_argument('thread', type=identifier)
    desktop = sub.add_parser('desktop', help='原生控制台的本机 JSON 接口')
    desktop.add_argument('operation', choices=('snapshot','action'))
    desktop.add_argument('--offset', type=int, default=0)
    queue = sub.add_parser('queue', help='macOS 多聊天与原生收件箱共享队列')
    commands = queue.add_subparsers(dest='queue_command', required=True)
    add = commands.add_parser('add', help='按参数顺序加入明确选定的聊天；不会启动调度')
    add.add_argument('threads', nargs='+', type=identifier)
    add.add_argument('--max-resumes', type=positive, default=3)
    inbox = commands.add_parser('inbox', help='连接新版原生收件箱数据目录')
    inbox.add_argument('root', type=Path)
    for command in ('status', 'pause', 'allow'):
        commands.add_parser(command)
    remove = commands.add_parser('remove')
    remove.add_argument('seq', type=int)
    release = commands.add_parser('release', help='人工确认当前项已结束后，释放占用并移出队列')
    release.add_argument('seq', type=int)
    release.add_argument('--verified-idle', action='store_true', required=True)
    run = commands.add_parser('run', help='前台运行；重启后从持久化状态复查')
    run.add_argument('--once', action='store_true')
    run.add_argument('--limit-id', default=None)
    run.add_argument('--lock-fd', type=int, default=None, help=argparse.SUPPRESS)
    run.add_argument('--ready-token', default=None, help=argparse.SUPPRESS)
    stop = sub.add_parser('stop', help='停止后续自动续跑，不打断正在执行的 App 任务')
    stop.add_argument('thread', type=identifier)
    for name in ('start', '_watch'):
        item = sub.add_parser(name, help='后台监控一个选定任务' if name == 'start' else '内部后台命令，请使用 start 启动')
        item.add_argument('thread', type=identifier)
        item.add_argument('--max-resumes', type=positive, default=3, help='该任务累计尝试次数上限（默认 3）')
        item.add_argument('--limit-id', default=None, help='多个额度桶时显式选择；单 codex 桶自动识别')
        if name == '_watch':
            item.add_argument('--lock-fd', type=int, default=None, help=argparse.SUPPRESS)
    return p


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def watch_process_spec(args, lock_fd=None):
    """Build an independent watcher command for the source checkout."""
    cmd = [sys.executable, '-m', 'codex_resume', '--home', str(args.home), '--app', str(args.app),
                    '--state-dir', str(args.state_dir), '_watch', args.thread,
                    '--max-resumes', str(args.max_resumes)]
    if lock_fd is not None:
        cmd += ['--lock-fd', str(lock_fd)]
    if args.limit_id:
        cmd += ['--limit-id', args.limit_id]
    return cmd, Path(__file__).resolve().parent.parent, None


def notify_user(thread_id, status, reason):
    title = 'Codex Auto Resume 需要确认'
    body = f'{status}: {reason} ({thread_id})'
    try:
        subprocess.run(['/usr/bin/osascript',
                        '-e', 'on run argv',
                        '-e', 'display notification (item 2 of argv) with title (item 1 of argv)',
                        '-e', 'end run',
                        title, body],
                       check=False, timeout=5, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def serve_watch(args, store):
    if platform_name() == 'windows' and args.lock_fd is None:
        for attempt in range(50):
            try:
                store.lock(args.thread)
                break
            except RuntimeError:
                if attempt == 49:
                    raise
                time.sleep(0.1)
    else:
        store.lock(args.thread, inherited_fd=args.lock_fd)
    quota = None
    def read_quota():
        nonlocal quota
        if quota is None:
            reader = ReadOnlyServer(codex_binary(args.app), args.home)
            reader.__enter__()
            quota = reader
        try:
            return quota.query('account/rateLimits/read')
        except Exception:
            quota.__exit__(None, None, None)
            quota = None
            raise
    def load_original(tid):
        # Revalidate eligibility before navigation as well as before dispatch.
        with ReadOnlyServer(codex_binary(args.app), args.home) as reader:
            if any(r['id'] == tid for r in list_conversations(reader, (True,))):
                return False
            saved = reader.query('thread/read', {'threadId': tid, 'includeTurns': True})
        if not stored_assessment(saved.get('thread'), tid)['canMonitor']:
            return False
        if not store.get(tid)['enabled']:
            return False
        open_selected_thread(args.app, tid)
    controller = Controller(args.thread, store, lambda: Desktop(args.home, args.app), read_quota, args.limit_id,
                            open_thread=load_original,
                            notify=lambda status, reason: notify_user(args.thread, status, reason))
    previous = None
    try:
        while controller.step():
            state = store.get(args.thread)
            status = (state['status'], state['reason'])
            if status != previous:
                output({'time': time.time(), 'status': status[0], 'reason': status[1]})
                previous = status
            # Small cancellable sleeps; after wake from system sleep step() rechecks everything.
            for _ in range(30):
                if not store.get(args.thread)['enabled']:
                    return
                time.sleep(1)
        state = store.get(args.thread)
        output({'status': state['status'], 'reason': state['reason']})
    finally:
        if quota:
            quota.__exit__(None, None, None)


def queue_command(args):
    if platform_name() != 'macos':
        raise RuntimeError('共享收件箱队列目前仅支持 macOS')
    from .queueing import QueueStore, Scheduler, AutoScanner, account_key, execution_lease, inbox_state, private_json
    store = QueueStore(args.state_dir)
    try:
        command = args.queue_command
        if command in ('add', 'inbox'):
            if not store.items():
                store.set_paused(True)
            if command == 'add':
                check_version(args.app)
                # Validate every explicit selection before enrolling the batch.
                for tid in args.threads:
                    assessment = inspect_task(args.home, args.app, tid)
                    if not assessment['canMonitor']:
                        raise RuntimeError(f"{tid}: {assessment['reason']}")
                    if store.uncertain(tid) or store.count(tid) >= args.max_resumes:
                        raise RuntimeError(f'{tid}: 存在不确定发送或累计预算已用尽')
                for tid in args.threads:
                    store.enqueue_chat(tid, args.max_resumes)
            else:
                store.enqueue_inbox(args.root)
            output({'items': store.items(), 'paused': store.paused(),
                    'note': '已保存选择；未启动后台进程。用 queue allow、queue run 运行。'})
        elif command == 'status':
            output({'items': store.items(), 'active': store.active(), 'paused': store.paused(),
                    'note': '状态为本机记录；不代表调度进程或 App 正在运行'})
        elif command in ('pause', 'allow'):
            store.set_paused(command == 'pause')
            output({'paused': store.paused(), 'note': '只影响未来派发，不中断已运行的回合'})
        elif command in ('remove', 'release'):
            with execution_lease(store.directory) as acquired:
                if not acquired:
                    raise RuntimeError('执行槽仍被使用，不能移除或释放')
                if command == 'release':
                    active = store.active()
                    if not active or active['seq'] != args.seq:
                        raise RuntimeError('该序号并未占用执行槽')
                    if active['kind'] == 'chat':
                        with Desktop(args.home, args.app, read_only=True) as app:
                            snapshot = app.snapshot(active['target'])
                            decision = decide(snapshot)
                        if (decision.action == 'wait' or snapshot.get('queuedFollowUps') or
                                snapshot.get('unconfirmedTurnSubmissions')):
                            raise RuntimeError('App 仍在执行或同步，不能释放')
                    else:
                        data = inbox_state(active['target'])
                        if any(t['status'] in ('running','preparing','uncertain') for t in data['tasks']):
                            raise RuntimeError('先在收件箱核对所有未结算操作，不能直接释放')
                    store.set_active(None)
                store.remove(args.seq)
            output({'removed': args.seq})
        elif command == 'run':
            check_version(args.app)
            # A distinct lock for the runner, shared dispatch.lock with native
            # inbox workers. Holding one must not imply holding the other.
            runner = Store(store.directory)
            try:
                runner.lock('00000000-0000-4000-8000-000000000001', inherited_fd=args.lock_fd)
                if args.ready_token:
                    private_json(store.directory / 'queue-runner.json',
                                 {'pid':os.getpid(), 'token':str(uuid.UUID(args.ready_token))})
                def read_quota():
                    with ReadOnlyServer(codex_binary(args.app), args.home) as server:
                        account = server.query('account/read', {'refreshToken': False})
                        result = server.query('account/rateLimits/read')
                        result['_accountKey'] = account_key(account, result)
                        return result
                from .drafts import read_drafts
                def archived(thread_id):
                    with ReadOnlyServer(codex_binary(args.app), args.home) as server:
                        return any(row['id'] == thread_id for row in list_conversations(server, (True,)))
                def reopen_chat(thread_id):
                    with ReadOnlyServer(codex_binary(args.app), args.home) as server:
                        if any(row['id'] == thread_id for row in list_conversations(server, (True,))):
                            return False
                        saved = server.query('thread/read', {'threadId':thread_id, 'includeTurns':True})
                    if not stored_assessment(saved.get('thread'), thread_id)['canMonitor']:
                        return False
                    if store.paused() or not store.get(thread_id) or not store.get(thread_id)['enabled']:
                        return False
                    open_selected_thread(args.app, thread_id)
                    return True
                scheduler = Scheduler(store, lambda: Desktop(args.home, args.app, read_only=False),
                                      read_quota, args.limit_id, drafts_reader=lambda: read_drafts(args.home),
                                      archived_reader=archived, reopen_chat=reopen_chat)
                # Login services can load thread history more slowly than an
                # interactive terminal. Keep the normal bounded RPC timeout;
                # discovery also has its own overall deadline.
                scanner = AutoScanner(store, lambda:ReadOnlyServer(codex_binary(args.app), args.home, timeout=25), read_quota)
                previous = None
                while True:
                    # Updating the App invalidates inbox grants as well as chats.
                    check_version(args.app)
                    try:
                        scan = scanner.poll()
                    except Exception:
                        scan = 'waiting_discovery'
                    state = scheduler.tick()
                    if scan == 'waiting_discovery' and state == 'idle':
                        state = scan
                    if state != previous or args.once:
                        output({'queue': state, 'active': store.active(), 'paused': store.paused()})
                        previous = state
                    if args.once:
                        break
                    time.sleep(5)
            finally:
                store.revoke_grant()
                runner.close()
    finally:
        store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    args.home, args.app, args.state_dir = [p.expanduser().absolute() for p in (args.home, args.app, args.state_dir)]
    if args.command == 'desktop':
        from .desktop_api import DesktopAPI
        api = DesktopAPI(args.home, args.app, args.state_dir, live_factory=Desktop)
        if args.operation == 'snapshot':
            output(api.snapshot(args.offset))
        else:
            raw = sys.stdin.buffer.read(140001)
            if len(raw) > 140000:
                raise ValueError('请求过大')
            request = json.loads(raw)
            if not isinstance(request, dict) or not isinstance(request.get('action'), str):
                raise ValueError('请求无效')
            output(api.action(request['action'],request.get('payload',{})))
        return
    if args.command == 'queue':
        return queue_command(args)
    if args.command == 'web':
        from .web import serve
        serve(args)
        return
    if args.command in ('doctor', 'check', 'list'):
        version = check_version(args.app, allow_unverified=True)
    elif args.command in ('start', '_watch'):
        version = check_version(args.app)
    if args.command == 'check':
        if version.get('diagnosticOnly'):
            # Never navigate, use history to enroll, or interpret unknown state
            # semantics as permission to monitor/send on an unverified build.
            try:
                with Desktop(args.home, args.app, read_only=True) as client:
                    client.snapshot(args.thread)
            except (AppError, OSError, TimeoutError) as exc:
                detail = str(exc) if isinstance(exc, AppError) else '连接不可用或超时'
                raise AppError(f"{version['compatibilityReason']}；只读检查失败：{detail}") from exc
            output({'threadId': args.thread, 'app': version, 'source': 'live',
                    'connection': 'connected', 'decision': 'stop', 'taskState': 'unsupported',
                    'canMonitor': False, 'autoResumeAllowed': False,
                    'reason': version['compatibilityReason'], 'checkedAt': time.time(),
                    'note': '只读快照已收到；未验证任务状态语义及发送协议，没有开启托管或发送消息'})
            return
        result = inspect_task(args.home, args.app, args.thread)
        output(dict(result, note='只读检查；没有打开任务、开启托管或发送消息'))
        return
    if args.command in ('doctor', 'list'):
        try:
            with ReadOnlyServer(codex_binary(args.app), args.home) as server:
                if args.command == 'list':
                    output(list_conversations(server))
                    return
                with Desktop(args.home, args.app, read_only=True):
                    pass
                quota = server.query('account/rateLimits/read')
                ready, next_check, reason = quota_status(quota, time.time())
                output({'app': version, 'ipc': 'connected', 'quotaRead': 'ok',
                        'autoResumeAllowed': not version.get('diagnosticOnly', False),
                        'ready': ready, 'nextCheck': next_check,
                        'reason': version.get('compatibilityReason', reason), 'quotaReason': reason,
                        'rateLimits': quota.get('rateLimits'),
                        'note': '只读探测成功不代表某个具体任务已可续跑'})
        except (RuntimeError, OSError, TimeoutError) as exc:
            if version.get('diagnosticOnly'):
                detail = str(exc) if isinstance(exc, AppError) else '连接或只读查询失败，请检查 App 安装及协议'
                raise AppError(f"{version['compatibilityReason']}；只读诊断失败：{detail}") from exc
            raise
        return
    store = Store(args.state_dir)
    try:
        if args.command == 'status':
            rows = store.all()
            for row in rows:
                row['attempts'] = store.count(row['thread_id'])
                if row.get('pid') and row['enabled']:
                    row['process'] = ('pid_exists_not_identity_verified' if process_exists(row['pid'])
                                      else 'not_running')
            output(rows)
        elif args.command == 'stop':
            store.stop(args.thread)
            output({'stopped': args.thread, 'note': '只取消未来自动续跑；已经发送的消息不会被撤回'})
        elif args.command == '_watch':
            serve_watch(args, store)
        elif args.command == 'start':
            if (store.directory / 'queue-managed').exists():
                raise RuntimeError('共享队列已接管调度；请使用 queue add/run，避免独立监控同时派发')
            # Validate the exact thread before arming a background process.
            assessment = inspect_task(args.home, args.app, args.thread)
            if not assessment['canMonitor']:
                raise RuntimeError(assessment['reason'])
            if store.uncertain(args.thread):
                raise RuntimeError('此任务有不确定的发送记录；先人工检查，不能自动重新接管')
            if store.count(args.thread) >= args.max_resumes:
                raise RuntimeError('累计尝试次数已达到上限；检查记录后再决定是否提高 --max-resumes')
            store.lock(args.thread)
            current = store.get(args.thread)
            if current and current['enabled']:
                raise RuntimeError('该任务已有一个监控进程或待确认的启动记录')
            store.arm(args.thread, args.max_resumes)
            # Keep the same flock held across process creation. The child adopts
            # this descriptor; closing only the parent's copy cannot unlock it.
            windows = platform_name() == 'windows'
            lock_fd = None if windows else store.lock_file.fileno()
            log = args.state_dir / (args.thread + '.log')
            cmd, child_cwd, child_env = watch_process_spec(args, lock_fd)
            try:
                if log.is_symlink():
                    raise RuntimeError('拒绝使用符号链接日志文件')
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(log, flags, 0o600)
                with os.fdopen(fd, 'ab') as stream:
                    options = dict(cwd=child_cwd, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=stream, start_new_session=True)
                    if windows:
                        options.pop('start_new_session')
                        options['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0x00000008)
                                                    | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200))
                    elif lock_fd is not None:
                        options['pass_fds'] = (lock_fd,)
                    child = subprocess.Popen(cmd, **options)
                if windows:
                    # The child retries this exact lock while the parent's copy is
                    # released. The enabled ledger row blocks a competing start.
                    store.release_lock()
            except Exception:
                store.update(args.thread, 'blocked', '后台进程启动失败；未启用托管', True,
                             only_enabled=True)
                raise
            # Verify child liveness and its first ledger update, rather than claim launch from Popen alone.
            for _ in range(40):
                state = store.get(args.thread)
                if state['status'] != 'starting' or child.poll() is not None:
                    break
                time.sleep(0.1)
            if child.poll() is not None:
                if store.get(args.thread)['status'] == 'starting':
                    store.update(args.thread, 'blocked', '后台进程启动失败', True, only_enabled=True)
                raise RuntimeError('后台监控已停止，请用 status 查看原因')
            output({'threadId': args.thread, 'pid': child.pid, 'log': str(log),
                    'status': store.get(args.thread)['status'],
                    'note': '仅所选任务；不会自动批准操作。App 必须保持打开。'})
    finally:
        store.close()


def cli():
    try:
        main()
    except KeyboardInterrupt:
        print('操作已中止', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # Known local errors contain no session content. Other exceptions stay generic.
        from .app import AppError
        if isinstance(exc, (AppError, RuntimeError, ValueError)):
            print(str(exc), file=sys.stderr)
        else:
            print('操作失败；请检查 App、目录权限和网络，未自动重试发送。', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    cli()
