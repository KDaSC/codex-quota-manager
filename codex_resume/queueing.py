"""Explicit FIFO jobs, one shared execution slot, and native inbox grants.

No background process is installed. The queue runs only through `queue run`.
The native inbox remains the sole writer of its own state and draft files.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from .controller import Controller
from .app import ThreadUnavailable
from .policy import decide, fingerprint, quota_status
from .store import Store


@contextmanager
def execution_lease(directory):
    import fcntl
    path = Path(directory) / 'dispatch.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def private_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.queue-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def account_key(result, limits):
    account = result.get('account')
    if not isinstance(account, dict) or account.get('type') != 'chatgpt':
        raise RuntimeError('共享队列要求现有 ChatGPT 登录；不会切换到 API 计费')
    # Same identity rule as IdeaInbox's CodexConnection.observe().
    identity = limits.get('accountId') or account.get('email')
    if not isinstance(identity, str) or not identity:
        raise RuntimeError('无法确认当前账户身份，未派发')
    return hashlib.sha256(identity.encode()).hexdigest()


class QueueStore(Store):
    def __init__(self, directory):
        super().__init__(directory)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS queue_items (
          seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
          target TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
          baseline TEXT, UNIQUE(kind,target)
        );
        CREATE TABLE IF NOT EXISTS queue_control (
          id INTEGER PRIMARY KEY CHECK(id=1), active INTEGER, account_key TEXT
        );
        INSERT OR IGNORE INTO queue_control(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS queue_preferences (
          id INTEGER PRIMARY KEY CHECK(id=1), reserve INTEGER NOT NULL DEFAULT 20,
          optional_enabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO queue_preferences(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS task_jobs (
          id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL,
          prompt TEXT NOT NULL, draft_fingerprint TEXT, status TEXT NOT NULL,
          turn_id TEXT, reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ignored_drafts (fingerprint TEXT PRIMARY KEY);
        CREATE UNIQUE INDEX IF NOT EXISTS task_jobs_draft_once ON task_jobs(draft_fingerprint)
          WHERE draft_fingerprint IS NOT NULL AND status != 'cancelled';
        CREATE TABLE IF NOT EXISTS auto_control (
          id INTEGER PRIMARY KEY CHECK(id=1), global_enabled INTEGER NOT NULL DEFAULT 0
        );
        INSERT OR IGNORE INTO auto_control(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS auto_chat_preferences (
          thread_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auto_seen (
          thread_id TEXT NOT NULL, failed_turn TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(thread_id,failed_turn)
        );
        CREATE TABLE IF NOT EXISTS auto_health (
          id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL DEFAULT 'unknown', checked_at REAL
        );
        INSERT OR IGNORE INTO auto_health(id) VALUES(1);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(queue_items)')}
        if 'origin' not in columns:
            self.db.execute("ALTER TABLE queue_items ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'")

    def global_auto(self):
        return bool(self.db.execute('SELECT global_enabled FROM auto_control WHERE id=1').fetchone()[0])

    def set_global_auto(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('自动续跑开关无效')
        with self.db:
            self.db.execute('UPDATE auto_control SET global_enabled=? WHERE id=1', (int(enabled),))

    def set_chat_auto(self, thread_id, enabled):
        thread_id = str(uuid.UUID(thread_id))
        if type(enabled) is not bool:
            raise ValueError('单聊天开关无效')
        with self.db:
            self.db.execute('''INSERT INTO auto_chat_preferences(thread_id,enabled) VALUES(?,?)
                ON CONFLICT(thread_id) DO UPDATE SET enabled=excluded.enabled''', (thread_id,int(enabled)))

    def selected_auto_chats(self):
        return {row['thread_id'] for row in self.db.execute('SELECT thread_id FROM auto_chat_preferences WHERE enabled=1')}

    def auto_health(self):
        return dict(self.db.execute('SELECT status,checked_at FROM auto_health WHERE id=1').fetchone())

    def set_auto_health(self, status, checked_at):
        if status not in ('ok','unavailable','unknown') or (status == 'unknown' and checked_at is not None):
            raise ValueError('自动检测状态无效')
        with self.db:
            self.db.execute('UPDATE auto_health SET status=?,checked_at=? WHERE id=1',(status,checked_at))

    def auto_authorized(self, thread_id):
        return self.global_auto() or thread_id in self.selected_auto_chats()

    def chat_allowed(self, thread_id):
        row = self.db.execute("SELECT origin FROM queue_items WHERE kind='chat' AND target=? AND enabled=1", (thread_id,)).fetchone()
        return bool(row and (row['origin'] != 'auto' or self.auto_authorized(thread_id)))

    def enroll_candidate(self, thread_id, turn_id, now):
        thread_id = str(uuid.UUID(thread_id))
        if not isinstance(turn_id,str) or not turn_id:
            return False
        if self.paused() or not self.auto_authorized(thread_id):
            return False
        (self.directory / 'queue-managed').touch(mode=0o600, exist_ok=True)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if (self.paused() or not self.auto_authorized(thread_id) or self.uncertain(thread_id) or
                    self.db.execute('SELECT 1 FROM auto_seen WHERE thread_id=? AND failed_turn=?',
                                    (thread_id,turn_id)).fetchone()):
                self.db.rollback(); return False
            watch=self.get(thread_id)
            item=self.db.execute("SELECT * FROM queue_items WHERE kind='chat' AND target=?", (thread_id,)).fetchone()
            if ((watch and not watch['enabled'] and watch['status'] != 'stopped') or
                    (item and not item['enabled'] and item['status'] != 'stopped') or self.count(thread_id)>=3):
                self.db.rollback(); return False
            if not watch or not watch['enabled']:
                self.db.execute('''INSERT INTO watches VALUES (?,1,3,'starting','等待核对额度中断',NULL,?)
                    ON CONFLICT(thread_id) DO UPDATE SET enabled=1,status='starting',
                    reason='等待核对额度中断',pid=NULL,updated=excluded.updated''', (thread_id,now))
            if not item:
                self.db.execute("INSERT INTO queue_items(kind,target,origin) VALUES('chat',?,'auto')", (thread_id,))
            elif not item['enabled']:
                self.db.execute("UPDATE queue_items SET enabled=1,status='queued',reason='',baseline=NULL,origin='auto' WHERE seq=?", (item['seq'],))
            elif item['origin'] == 'auto':
                # A newly observed failed turn is a new candidate, not a
                # settings change within the previously monitored turn.
                # The scheduler still obtains and binds a fresh live state.
                self.db.execute("UPDATE queue_items SET status='queued',reason='',baseline=NULL WHERE seq=?", (item['seq'],))
            self.db.execute('INSERT INTO auto_seen VALUES (?,?,?)',(thread_id,turn_id,now))
            self.db.execute('''INSERT INTO quota_waits(thread_id,failed_turn,next_check,is_hard,poll_at)
                VALUES(?,?,?,1,?) ON CONFLICT(thread_id) DO UPDATE SET failed_turn=excluded.failed_turn,
                next_check=excluded.next_check,is_hard=1,poll_at=excluded.poll_at''',
                (thread_id,turn_id,now+60,now+60))
            self.db.commit(); return True
        except Exception:
            self.db.rollback(); raise

    def settings(self):
        row = self.db.execute('SELECT reserve,optional_enabled FROM queue_preferences WHERE id=1').fetchone()
        health = self.auto_health()
        return {'reserve': row['reserve'], 'closeoutBuffer': 10,
                'optionalEnabled': bool(row['optional_enabled']), 'globalAuto':self.global_auto(),
                'autoHealth':health['status'], 'autoHealthCheckedAt':health['checked_at']}

    def set_reserve(self, value):
        if type(value) is not int or value not in (10, 20, 30, 40):
            raise ValueError('保留线只能选择10%、20%、30%或40%')
        self.revoke_grant()
        with self.db:
            self.db.execute('UPDATE queue_preferences SET reserve=? WHERE id=1', (value,))
            self.db.execute('DELETE FROM quota_waits WHERE is_hard=0')
        # Invalidate any permit issued using the previous settings.
        self.revoke_grant()

    def dispatch_minimum(self):
        return self.settings()['reserve'] + 10

    def add_task(self, thread_id, prompt, kind, draft_fingerprint=None):
        thread_id = str(uuid.UUID(thread_id))
        if kind not in ('normal', 'optional') or not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 12000:
            raise ValueError('任务类型或内容无效')
        if draft_fingerprint is not None and (not isinstance(draft_fingerprint, str) or not draft_fingerprint):
            raise ValueError('草稿身份无效')
        if draft_fingerprint and self.db.execute('SELECT 1 FROM task_jobs WHERE draft_fingerprint=? AND status!=?',
                                                 (draft_fingerprint, 'cancelled')).fetchone():
            raise RuntimeError('此版本草稿已加入任务')
        task_id = str(uuid.uuid4())
        now = time.time()
        if kind == 'normal':
            (self.directory / 'queue-managed').touch(mode=0o600, exist_ok=True)
        with self.db:
            self.db.execute('INSERT INTO task_jobs VALUES (?,?,?,?,?,?,NULL,?,?,?)',
                            (task_id, thread_id, kind, prompt, draft_fingerprint,
                             'queued' if kind == 'normal' else 'stored', '', now, now))
            if kind == 'normal':
                self.db.execute("INSERT INTO queue_items(kind,target) VALUES('task',?)", (task_id,))
        return task_id

    def task(self, task_id):
        row = self.db.execute('SELECT * FROM task_jobs WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return dict(row)

    def tasks(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM task_jobs ORDER BY created,id')]

    def arm_task(self, task_id):
        job = self.task(task_id)
        if job['kind'] != 'optional' or job['status'] != 'stored':
            raise RuntimeError('只有未选择的可选任务可以加入执行队列')
        (self.directory / 'queue-managed').touch(mode=0o600, exist_ok=True)
        with self.db:
            self.db.execute("UPDATE task_jobs SET status='queued',updated=? WHERE id=?", (time.time(), task_id))
            self.db.execute("INSERT INTO queue_items(kind,target) VALUES('task',?)", (task_id,))

    def update_task(self, task_id, status, reason='', turn_id=None):
        with self.db:
            self.db.execute('UPDATE task_jobs SET status=?,reason=?,turn_id=COALESCE(?,turn_id),updated=? WHERE id=?',
                            (status, reason, turn_id, time.time(), task_id))

    def ignore_draft(self, fingerprint):
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError('草稿身份无效')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO ignored_drafts VALUES (?)', (fingerprint,))

    def known_draft(self, fingerprint):
        return bool(self.db.execute('SELECT 1 FROM ignored_drafts WHERE fingerprint=?', (fingerprint,)).fetchone() or
                    self.db.execute("SELECT 1 FROM task_jobs WHERE draft_fingerprint=? AND status!='cancelled'", (fingerprint,)).fetchone())

    def enqueue_chat(self, thread_id, maximum=3):
        thread_id = str(uuid.UUID(thread_id))
        if self.uncertain(thread_id):
            raise RuntimeError('该聊天有未确认发送；先在原聊天核对')
        if self.count(thread_id) >= maximum:
            raise RuntimeError('累计续跑预算已用尽')
        existing = self.get(thread_id)
        # Enrolling twice must not reset the controller's persisted state.
        if not existing or not existing['enabled']:
            self.arm(thread_id, maximum)
            with self.db:
                self.db.execute("UPDATE queue_items SET baseline=NULL,status='queued',reason='' WHERE kind='chat' AND target=?", (thread_id,))
        self._enqueue('chat', thread_id)

    def enqueue_inbox(self, root):
        root = Path(root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError('收件箱数据目录不存在')
        marker = root / '.codex-resume-queue.json'
        if marker.exists() and json.loads(marker.read_text()).get('directory') != str(self.directory.resolve()):
            raise RuntimeError('这个收件箱已连接到另一个队列')
        private_json(marker, {'schemaVersion': 1, 'directory': str(self.directory.resolve())})
        self._enqueue('inbox', str(root))

    def _enqueue(self, kind, target):
        with self.db:
            self.db.execute('''INSERT INTO queue_items(kind,target) VALUES(?,?)
                ON CONFLICT(kind,target) DO UPDATE SET enabled=1''', (kind, target))
        # This marker disables the upstream independent per-chat watchers.
        (self.directory / 'queue-managed').touch(mode=0o600, exist_ok=True)

    def items(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM queue_items ORDER BY seq')]

    def active(self):
        row = self.db.execute('''SELECT q.* FROM queue_items q JOIN queue_control c
            ON q.seq=c.active WHERE c.id=1''').fetchone()
        return dict(row) if row else None

    def set_active(self, item):
        with self.db:
            self.db.execute('UPDATE queue_control SET active=? WHERE id=1',
                            (item['seq'] if item else None,))
        if not item or item['kind'] != 'inbox':
            self.revoke_grant()

    def update_item(self, item, status, reason='', disable=False, baseline=None):
        with self.db:
            self.db.execute('''UPDATE queue_items SET status=?,reason=?,
                enabled=CASE WHEN ? THEN 0 ELSE enabled END,
                baseline=COALESCE(?,baseline) WHERE seq=?''',
                (status, reason, disable, baseline, item['seq']))

    def remove(self, seq):
        item = next((r for r in self.items() if r['seq'] == seq), None)
        if not item:
            raise ValueError('队列序号不存在')
        if self.active() and self.active()['seq'] == seq:
            raise RuntimeError('该项仍占用执行槽；先暂停并核对原聊天/收件箱，使用 release 确认已结束')
        self.update_item(item, 'paused', '用户移出调度', disable=True)
        if item['kind'] == 'chat':
            self.stop(item['target'])

    def paused(self):
        return (self.directory / 'queue.paused').exists()

    def set_paused(self, paused):
        path = self.directory / 'queue.paused'
        if paused:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            self.revoke_grant()
        else:
            path.unlink(missing_ok=True)

    def revoke_grant(self):
        private_json(self.directory / 'inbox-grant.json', {'schemaVersion': 1, 'allowed': False})

    def grant(self, item, task_id, now):
        key = self.db.execute('SELECT account_key FROM queue_control WHERE id=1').fetchone()[0]
        private_json(self.directory / 'inbox-grant.json', {
            'schemaVersion': 1, 'allowed': True, 'root': item['target'],
            'taskID': task_id, 'accountKey': key, 'minimumRemaining': self.dispatch_minimum(),
            'issuedAt': now, 'expiresAt': now + 60})

    def bind_account(self, key):
        if not isinstance(key, str) or not key:
            raise RuntimeError('无法确认额度所属账户')
        old = self.db.execute('SELECT account_key FROM queue_control WHERE id=1').fetchone()[0]
        if old and old != key:
            self.set_paused(True)
            raise RuntimeError('账户已变化，共享队列已暂停；请恢复原账户后复查')
        with self.db:
            self.db.execute('UPDATE queue_control SET account_key=? WHERE id=1', (key,))


def inbox_state(root):
    path = Path(root) / 'state.json'
    if not path.exists() and not path.is_symlink() and path.parent.is_dir():
        return {'schemaVersion': 1, 'stopped': False, 'tasks': []}
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise RuntimeError('收件箱状态文件类型或大小不受支持')
    state = json.loads(path.read_text())
    if (state.get('schemaVersion') != 1 or type(state.get('stopped')) is not bool
            or not isinstance(state.get('tasks'), list)):
        raise RuntimeError('收件箱状态版本不受支持')
    for task in state['tasks']:
        if (not isinstance(task, dict) or not isinstance(task.get('id'), str)
                or type(task.get('automatic')) is not bool or not isinstance(task.get('status'), str)):
            raise RuntimeError('收件箱任务状态不完整')
    return state


class AutoScanner:
    """Bounded read-only discovery; it can enroll, never send a message."""
    def __init__(self, store, server_factory, quota_reader, clock=time.time, discover=None):
        self.store, self.server_factory, self.quota_reader = store, server_factory, quota_reader
        self.clock = clock
        if discover is None:
            from .tasks import discover_quota_candidates
            discover = discover_quota_candidates
        self.discover = discover
        self.next_scan = 0

    def poll(self):
        if self.store.paused():
            return 'paused'
        selected = self.store.selected_auto_chats()
        if not self.store.global_auto() and not selected:
            return 'disabled'
        if self.clock() < self.next_scan:
            return 'waiting'
        self.next_scan = self.clock() + 60
        with execution_lease(self.store.directory) as acquired:
            if not acquired:
                return 'busy'
            if self.store.paused():
                return 'paused'
            try:
                quota = self.quota_reader()
                self.store.bind_account(quota.get('_accountKey'))
                selected = None if self.store.global_auto() else self.store.selected_auto_chats()
                with self.server_factory() as server:
                    candidates = self.discover(server, selected)
                for thread_id, turn_id in candidates:
                    if self.store.paused():
                        break
                    self.store.enroll_candidate(thread_id, turn_id, self.clock())
            except Exception:
                self.store.set_auto_health('unavailable', self.clock())
                raise
            self.store.set_auto_health('ok', self.clock())
        return 'checked'


class Scheduler:
    def __init__(self, store, desktop_factory, quota_reader, limit_id=None, clock=time.time, drafts_reader=None, archived_reader=None, reopen_chat=None):
        self.store, self.desktop_factory, self.reader = store, desktop_factory, quota_reader
        self.clock, self.limit_id = clock, limit_id
        self.controllers = {}
        self.inbox_next_check = 0
        self.last_inbox_minimum = None
        self.drafts_reader = drafts_reader
        self.archived_reader = archived_reader
        self.reopen_chat = reopen_chat
        self.next_reopen = {}

    def read_quota(self):
        result = self.reader()
        self.store.bind_account(result.get('_accountKey'))
        return result

    def tick(self):
        with execution_lease(self.store.directory) as acquired:
            if not acquired:
                return 'busy'
            if self.store.paused():
                self.store.revoke_grant()
                return 'paused'
            all_items = self.store.items()
            items = [i for i in all_items if i['enabled']]
            # An uncertain prior send owns the slot across restarts, not just
            # until an arbitrary timeout. Never hand it to another job.
            # An uncertain send still blocks the queue after its individual
            # switch has been turned off; disabling is not confirmation.
            for item in all_items:
                if item['kind'] == 'task' and self.store.task(item['target'])['status'] in ('dispatching', 'uncertain'):
                    self.store.set_active(item)
                    self.store.update_task(item['target'], 'uncertain', '发送结果未确认，等待人工核对')
                    return 'uncertain'
                if item['kind'] == 'chat' and self.store.uncertain(item['target']):
                    self.store.set_active(item)
                    self.store.update_item(item, 'uncertain', '发送未确认，整队等待人工核对')
                    return 'uncertain'
                if item['kind'] == 'inbox' and item['enabled']:
                    try:
                        data = inbox_state(item['target'])
                    except (OSError, ValueError, RuntimeError):
                        self.store.update_item(item, 'waiting_inbox', '等待收件箱状态可读')
                        return 'waiting_inbox'
                    if any(t['status'] in ('uncertain', 'preparing', 'running') or
                           (t.get('operation') is not None and
                            (not isinstance(t['operation'], dict) or t['operation'].get('phase') != 'finished'))
                           for t in data['tasks']):
                        # A running integrated worker holds dispatch.lock. Seeing
                        # these states after acquiring it means the worker died or
                        # an unmanaged copy is running; either way never dispatch.
                        self.store.set_active(item)
                        self.store.update_item(item, 'uncertain', '收件箱存在未结算操作，等待核对')
                        self.store.revoke_grant()
                        return 'uncertain'
            active = self.store.active()
            inactive_chat = (active and active['kind']=='chat' and
                             (not active['enabled'] or not self.store.chat_allowed(active['target'])))
            if inactive_chat:
                # Switching off must not interrupt an already-running turn or
                # leave the whole queue blocked after that turn finishes.
                try:
                    with self.desktop_factory() as desktop:
                        current = decide(desktop.snapshot(active['target']))
                except Exception:
                    return 'needs_attention'
                if current.task_state == 'running':
                    return 'running'
                if current.task_state not in ('idle','quota_limited','interrupted','other_failure'):
                    return 'needs_attention'
                self.store.update_item(active, 'paused', '自动续跑已关闭，原回合已停止或完成')
                self.store.set_active(None)
                active = None
            if active and not active['enabled']:
                return 'needs_attention'
            items = [item for item in items if item['kind'] != 'chat' or self.store.chat_allowed(item['target'])]
            try:
                # Observe EVERY enrolled chat before authorizing any new work.
                # Unknown/unloaded owners block rather than guessing idle.
                states = {}
                running = {}
                for item in items:
                    if item['kind'] not in ('chat', 'task'):
                        continue
                    job = self.store.task(item['target']) if item['kind'] == 'task' else None
                    # A selected optional task that has never started must not
                    # block normal work merely because its chat is unloaded.
                    if job and job['kind'] == 'optional' and job['status'] == 'queued':
                        continue
                    tid = item['target'] if item['kind'] == 'chat' else job['thread_id']
                    with self.desktop_factory() as desktop:
                        try:
                            state = desktop.snapshot(tid)
                        except (ThreadUnavailable, TimeoutError):
                            if (item['kind'] == 'chat' and self.reopen_chat and not self.store.paused()
                                    and self.clock() >= self.next_reopen.get(tid, 0)):
                                self.next_reopen[tid] = self.clock() + 300
                                self.reopen_chat(tid)
                            return 'waiting_connection'
                    states[tid] = state
                    if decide(state).task_state == 'running':
                        running.setdefault(tid, []).append(item)
                if len(running) > 1:
                    self.store.revoke_grant()
                    return 'multiple_running'
                if running:
                    choices = next(iter(running.values()))
                    running_item = next((i for i in choices if active and i['seq'] == active['seq']), choices[0])
                    self.store.set_active(running_item)
                    self.store.update_item(running_item, 'watching', '所选聊天正在执行，保留执行槽')
                    return 'running'
                candidates = [active] if active else sorted(items, key=lambda x: (
                    x['kind'] == 'task' and self.store.task(x['target'])['kind'] == 'optional', x['seq']))
                for item in candidates:
                    if item['kind'] == 'inbox':
                        data = inbox_state(item['target'])
                        if data['stopped']:
                            if active:
                                self.store.set_active(None)
                            self.store.revoke_grant()
                            self.store.update_item(item, 'paused', '收件箱已暂停')
                            continue
                        task = next((t for t in data['tasks'] if t['automatic'] and
                                     t['status'] in ('queued', 'waitingQuota', 'waitingConnection')), None)
                        if active:
                            grant_path = self.store.directory / 'inbox-grant.json'
                            grant = json.loads(grant_path.read_text()) if grant_path.exists() else {}
                            selected = next((t for t in data['tasks'] if t['id'] == grant.get('taskID')), None)
                            if selected and selected['status'] not in ('queued', 'waitingQuota', 'waitingConnection'):
                                self.store.set_active(None)
                                self.store.update_item(item, selected['status'], '收件箱已写回所选任务结果')
                                return 'settled'
                        if task:
                            minimum = self.store.dispatch_minimum()
                            if self.last_inbox_minimum != minimum:
                                self.inbox_next_check = 0
                                self.last_inbox_minimum = minimum
                            if self.clock() < self.inbox_next_check:
                                return 'waiting_quota'
                            ready, next_check, reason = quota_status(self.read_quota(), self.clock(), self.limit_id,
                                                                     minimum_remaining=minimum, require_secondary=True)
                            if not ready:
                                self.inbox_next_check = min(next_check or self.clock() + 900, self.clock() + 900)
                                self.store.revoke_grant()
                                self.store.update_item(item, 'waiting_quota', reason)
                                return 'waiting_quota'
                            self.store.set_active(item)
                            self.store.grant(item, task['id'], self.clock())
                            self.store.update_item(item, 'waiting_inbox', '执行许可已发给原生收件箱；需保持新版应用打开')
                            return 'inbox_granted'
                        if active:
                            self.store.set_active(None)
                        self.store.update_item(item, 'idle', '没有选择自动整理的待办')
                        continue
                    if item['kind'] == 'task':
                        tid = self.store.task(item['target'])['thread_id']
                        if tid not in states:
                            with self.desktop_factory() as desktop:
                                states[tid] = desktop.snapshot(tid)
                            if decide(states[tid]).task_state == 'running':
                                self.store.set_active(item)
                                self.store.update_item(item, 'watching', '所选聊天正在执行，保留执行槽')
                                return 'running'
                        result = self.step_task(item, states)
                        if result == 'settled':
                            return result
                        return result
                    tid = item['target']
                    decision = decide(states[tid])
                    if decision.action == 'resume':
                        baseline = fingerprint(states[tid])
                        if item['baseline'] and item['baseline'] != baseline:
                            self.store.stop(tid)
                            self.store.update_item(item, 'changed', '等待期间任务发生变化，请重新选择', disable=True)
                            self.store.set_active(None)
                            return 'changed'
                        self.store.update_item(item, 'checking', baseline=baseline)
                    controller = self.controllers.get(tid)
                    if controller is None:
                        controller = Controller(tid, self.store, self.desktop_factory,
                                                self.read_quota, self.limit_id, clock=self.clock,
                                                queue_owned=True,
                                                authorization=lambda tid=tid:self.store.chat_allowed(tid))
                        self.controllers[tid] = controller
                    if decision.action == 'resume':
                        # Bind the preflight snapshot to the controller's next
                        # fresh read as well, including its very first tick.
                        controller.waiting_baseline = item['baseline'] or baseline
                    keep = controller.step()
                    watch = self.store.get(tid)
                    self.store.update_item(item, watch['status'], watch['reason'], disable=not keep)
                    if watch['status'] in ('resumed', 'uncertain'):
                        self.store.set_active(item)
                        # A new turn is fingerprinted independently after this one.
                        with self.store.db:
                            self.store.db.execute('UPDATE queue_items SET baseline=NULL WHERE seq=?', (item['seq'],))
                        return watch['status']
                    if not keep and active:
                        self.store.set_active(None)
                    if not keep:
                        self.controllers.pop(tid, None)
                    if keep:
                        return watch['status']
                return 'idle'
            except Exception:
                self.store.revoke_grant()
                # Do not serialize private IPC errors or conversation content.
                return 'waiting_connection' if not self.store.paused() else 'paused'

    def normal_pending(self):
        if any(t['kind'] == 'normal' and t['status'] not in ('completed', 'cancelled') for t in self.store.tasks()):
            return True
        for item in self.store.items():
            if item['kind'] == 'chat' and (item['enabled'] or item['status'] in ('needs_attention', 'blocked', 'changed', 'uncertain', 'budget')):
                return True
            if item['kind'] == 'inbox' and item['enabled']:
                data = inbox_state(item['target'])
                if any(t['automatic'] and t['status'] not in ('completed', 'cancelled', 'recorded') for t in data['tasks']):
                    return True
        if self.drafts_reader is None:
            return True
        drafts = self.drafts_reader()
        if isinstance(drafts, dict):
            drafts = drafts['drafts']
        return any(not self.store.known_draft(d['fingerprint']) for d in drafts)

    def original_draft_matches(self, job):
        if not job['draft_fingerprint']:
            return True
        if self.drafts_reader is None:
            return False
        drafts = self.drafts_reader()
        if isinstance(drafts, dict):
            drafts = drafts['drafts']
        return any(d.get('threadId') == job['thread_id'] and
                   d.get('fingerprint') == job['draft_fingerprint'] for d in drafts)

    def step_task(self, item, states):
        from .policy import explicit_task_allowed, latest_turn, quota_failure
        job = self.store.task(item['target'])
        tid = job['thread_id']
        state = states[tid]
        turn = latest_turn(state)
        if job['status'] == 'running':
            if turn.get('turnId') != job['turn_id']:
                self.store.update_task(job['id'], 'uncertain', '运行后聊天状态改变，需要核对')
                self.store.set_active(item)
                return 'uncertain'
            if turn.get('status') == 'completed':
                self.store.update_task(job['id'], 'completed', '原聊天回合已完成')
                self.store.update_item(item, 'completed', '任务已完成', disable=True)
                self.store.set_active(None)
                return 'settled'
            if turn.get('status') == 'failed' and quota_failure(turn):
                self.store.update_task(job['id'], 'waiting_quota', '因额度耗尽暂停，等待恢复')
                failed_baseline = fingerprint(state)
                self.store.update_item(item, 'waiting_quota', '因额度耗尽暂停，等待恢复', baseline=failed_baseline)
                item = dict(item, baseline=failed_baseline)
                watch = self.store.get(tid)
                if not watch or not watch['enabled']:
                    self.store.arm(tid, 3)
            else:
                self.store.update_task(job['id'], 'needs_attention', '任务被中断或失败，需要检查')
                self.store.update_item(item, 'needs_attention', '任务需要检查', disable=True)
                self.store.set_active(None)
                return 'needs_attention'
            job = self.store.task(job['id'])
        if job['status'] == 'waiting_quota':
            if not item['baseline']:
                self.store.update_task(job['id'], 'needs_attention', '缺少等待前状态，请在原聊天核对')
                self.store.update_item(item, 'needs_attention', '缺少等待前状态', disable=True)
                return 'needs_attention'
            controller = self.controllers.get('task:' + job['id'])
            if controller is None:
                controller = Controller(tid, self.store, self.desktop_factory, self.read_quota,
                                        self.limit_id, clock=self.clock, queue_owned=True)
                self.controllers['task:' + job['id']] = controller
            controller.waiting_baseline = item['baseline']
            keep = controller.step()
            watch = self.store.get(tid)
            if watch['status'] == 'resumed':
                attempt = self.store.attempt(tid, turn['turnId'])
                self.store.update_task(job['id'], 'running', '额度恢复后已在原聊天续跑', attempt['resumed_turn'])
                with self.store.db:
                    self.store.db.execute('UPDATE queue_items SET baseline=NULL WHERE seq=?', (item['seq'],))
                self.store.set_active(item)
                return 'resumed'
            if not keep:
                self.store.update_task(job['id'], 'uncertain' if watch['status'] == 'uncertain' else 'needs_attention', watch['reason'])
                return watch['status']
            return watch['status']
        if job['status'] != 'queued':
            return 'needs_attention'
        if job['kind'] == 'optional' and self.normal_pending():
            self.store.update_item(item, 'waiting_normal', '还有正常待办或待发送草稿')
            return 'waiting_normal'
        if job['draft_fingerprint']:
            if self.drafts_reader is None:
                return 'waiting_drafts'
            drafts = self.drafts_reader()
            if isinstance(drafts, dict):
                drafts = drafts['drafts']
            if not any(d.get('threadId') == tid and d.get('fingerprint') == job['draft_fingerprint'] for d in drafts):
                self.store.update_task(job['id'], 'needs_attention', '原草稿已变化，请重新确认')
                self.store.update_item(item, 'changed', '原草稿已变化', disable=True)
                return 'changed'
        if not explicit_task_allowed(state):
            self.store.update_task(job['id'], 'needs_attention', '聊天正在执行、等待输入或状态未知')
            self.store.update_item(item, 'needs_attention', '聊天需要检查', disable=True)
            return 'needs_attention'
        if self.archived_reader is not None and self.archived_reader(tid):
            self.store.update_task(job['id'], 'needs_attention', '聊天已归档，不能发送任务')
            self.store.update_item(item, 'needs_attention', '聊天已归档', disable=True)
            return 'needs_attention'
        reserve = self.store.settings()['reserve']
        minimum = reserve + (15 if job['kind'] == 'optional' else 10)
        ready, _, reason = quota_status(self.read_quota(), self.clock(), self.limit_id,
                                        minimum_remaining=minimum, allow_equal=job['kind'] == 'optional', require_secondary=True)
        if not ready:
            self.store.update_item(item, 'waiting_quota', reason)
            return 'waiting_quota'
        baseline = fingerprint(state)
        self.store.update_task(job['id'], 'dispatching', '已记录发送意图')
        self.store.set_active(item)
        try:
            with self.desktop_factory() as desktop:
                result = desktop.submit(tid, job['prompt'], baseline, job['id'],
                    dispatch_guard=lambda: not self.store.paused() and self.store.settings()['reserve'] == reserve and
                        self.store.task(job['id'])['status'] == 'dispatching' and
                        self.original_draft_matches(job) and
                        (job['kind'] != 'optional' or not self.normal_pending()))
            self.store.update_task(job['id'], 'running', '原聊天已开始', result)
            self.store.update_item(item, 'running', '原聊天已开始')
            return 'running'
        except Exception:
            self.store.update_task(job['id'], 'uncertain', '发送结果未确认，等待人工核对')
            return 'uncertain'
