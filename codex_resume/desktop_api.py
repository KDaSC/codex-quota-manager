"""Local JSON interface for the native control center; no cloud uploads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from .app import Desktop, check_version, confirmed_message
from .drafts import DraftUnavailable, read_drafts
from .queueing import QueueStore, execution_lease
from .rpc import ReadOnlyServer
from .runtime import codex_binary
from .store import Store
from .tasks import inspect_task
from .policy import decide, latest_turn, numeric


class DesktopAPI:
    def __init__(self, home, app, state_dir, server_factory=ReadOnlyServer, live_factory=None):
        self.home, self.app, self.state_dir = Path(home), Path(app), Path(state_dir)
        self.server_factory = server_factory
        self.live_factory = live_factory

    def _server(self):
        return self.server_factory(codex_binary(self.app), self.home)

    def runner_running(self):
        try:
            value = json.loads((self.state_dir / 'queue-runner.json').read_text())
            pid, token = int(value['pid']), str(uuid.UUID(value['token']))
            if pid <= 0:return False
            os.kill(pid, 0)
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='],
                                    capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL)
            return result.returncode == 0 and 'codex_resume' in result.stdout and ('--ready-token ' + token) in result.stdout
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return False

    def start_runner(self, store):
        check_version(self.app)
        if self.runner_running():
            store.set_paused(False)
            return {'started':True, 'existing':True}
        runner = Store(self.state_dir)
        try:
            runner.lock('00000000-0000-4000-8000-000000000001')
            # No background process may dispatch until this exact child owns
            # the inherited lock and reports its random ready token.
            store.set_paused(True)
            store.set_auto_health('unknown', None)
            token = str(uuid.uuid4())
            fd = runner.lock_file.fileno()
            command = [sys.executable, '-m', 'codex_resume', '--home', str(self.home), '--app', str(self.app),
                       '--state-dir', str(self.state_dir), 'queue', 'run', '--lock-fd', str(fd), '--ready-token', token]
            path = self.state_dir / 'queue-runner.log'
            if path.is_symlink():raise RuntimeError('拒绝使用符号链接日志')
            logfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(logfd, 'ab') as log:
                child = subprocess.Popen(command, cwd=Path(__file__).resolve().parent.parent,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, pass_fds=(fd,))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child.poll() is not None:raise RuntimeError('后台调度器启动失败，队列仍暂停')
                try:
                    ready = json.loads((self.state_dir / 'queue-runner.json').read_text())
                    if ready.get('pid') == child.pid and ready.get('token') == token:
                        store.set_paused(False)
                        return {'started':True,'existing':False,'pid':child.pid}
                except (OSError, ValueError, TypeError):
                    pass
                time.sleep(0.05)
            # The child might be running but unconfirmed; leave the durable
            # pause flag in place and never launch a second one automatically.
            raise RuntimeError('后台调度器未确认就绪，队列仍暂停')
        finally:runner.close()

    def _global(self):
        try:
            p = self.home / '.codex-global-state.json'
            if p.is_symlink() or p.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('unsupported state')
            value = json.loads(p.read_text())
            if not isinstance(value, dict):raise ValueError('unsupported state')
            return value
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _thread_list(server, archived=False):
        rows, cursor, seen = [], None, set()
        for _ in range(100):
            params = {'limit':100,'archived':archived,'sourceKinds':['cli','vscode','exec','appServer','unknown'],
                      'sortKey':'updated_at','modelProviders':[]}
            if cursor is not None:params['cursor'] = cursor
            page = server.query('thread/list',params)
            if not isinstance(page,dict) or not isinstance(page.get('data'),list) or 'nextCursor' not in page:
                raise RuntimeError('聊天列表不完整')
            rows.extend(page['data'])
            cursor = page['nextCursor']
            if cursor is None:return rows
            if not isinstance(cursor,str) or not cursor or cursor in seen:raise RuntimeError('聊天分页异常')
            seen.add(cursor)
        raise RuntimeError('聊天列表过长，未读取完整')

    def snapshot(self, offset=0):
        global_state = self._global()
        projects = global_state.get('local-projects', {})
        projects = projects if isinstance(projects, dict) else {}
        assignments = global_state.get('thread-project-assignments', {})
        assignments = assignments if isinstance(assignments, dict) else {}
        try:
            draft_snapshot = read_drafts(self.home)
            drafts_available = True
        except DraftUnavailable:
            draft_snapshot = {'drafts':[], 'observedAt':time.time(), 'sourceModifiedAt':None}
            drafts_available = False
        drafts = {d['threadId']:d for d in draft_snapshot['drafts'] if d['threadId']}
        threads, windows, inventory_error = [], [], None
        try:
            with self._server() as server:
                raw = [(row, archived) for archived in (False, True)
                       for row in self._thread_list(server, archived)]
                limits = server.query('account/rateLimits/read')
            bucket = (limits.get('rateLimitsByLimitId') or {}).get('codex') or limits.get('rateLimits') or {}
            for key in ('primary','secondary'):
                w = bucket.get(key)
                if isinstance(w,dict) and isinstance(w.get('usedPercent'),(int,float)) and not isinstance(w['usedPercent'],bool):
                    windows.append({'kind':key,'remaining':max(0,min(100,100-w['usedPercent'])),
                                    'duration':w.get('windowDurationMins'),'resetsAt':w.get('resetsAt')})
            for row, archived in raw:
                if not isinstance(row,dict) or not isinstance(row.get('id'),str):continue
                tid = row['id']
                project_id = row.get('projectId')
                if project_id not in projects:
                    assignment = assignments.get(tid)
                    project_id = assignment.get('projectId') if isinstance(assignment,dict) else None
                project = projects.get(project_id, {}) if project_id else {}
                if not isinstance(project,dict):project = {}
                if not project:
                    cwd = row.get('cwd')
                    if isinstance(cwd,str):
                        matches = [(len(root), value) for value in projects.values() if isinstance(value,dict)
                                   for root in value.get('rootPaths',[]) if isinstance(root,str)
                                   and (cwd == root or cwd.startswith(root.rstrip('/')+'/'))]
                        if matches:project = max(matches,key=lambda x:x[0])[1]
                status = row.get('status')
                status = status if isinstance(status,dict) else {}
                if status.get('type') == 'active':
                    status_name = 'needs_attention' if status.get('activeFlags') else 'running'
                elif status.get('type') == 'idle':status_name = 'idle'
                else:status_name = 'unknown'
                threads.append({'id':tid,'name':row.get('name') or '未命名聊天',
                                'projectName':project.get('name') or '其他聊天','status':status_name,
                                'archived':archived,'draft':drafts.pop(tid,None)})
        except Exception:
            inventory_error = '暂时无法读取完整聊天列表或额度，请检查 Codex 连接。'
        # Keep draft evidence visible even when a conversation isn't in the index.
        for tid,draft in drafts.items():
            threads.append({'id':tid,'name':'未能匹配的聊天','projectName':'其他聊天',
                            'status':'unknown','archived':False,'draft':draft})
        unbound = [d for d in draft_snapshot['drafts'] if not d['threadId']]
        # The separate read-only app-server can report notLoaded for a chat
        # that is active in the desktop App. Never infer idle from that value.
        # Rotate bounded read-only owner snapshots over the complete inventory.
        eligible = [row for row in threads if not row['archived']]
        next_offset = 0
        if self.live_factory and eligible:
            offset = max(0, int(offset)) % len(eligible)
            next_offset = (offset + min(25, len(eligible))) % len(eligible)
            priority = [r for r in eligible if r['draft']]
            rotated = (eligible[offset:] + eligible[:offset])[:25]
            selected = list({r['id']:r for r in priority + rotated}.values())
            deadline = time.monotonic() + 3
            try:
                with self.live_factory(self.home, self.app, timeout=0.4, read_only=True) as client:
                    for row in selected:
                        if time.monotonic() >= deadline:break
                        try:
                            state = client.snapshot(row['id'])
                            task_state = decide(state).task_state
                            row['status'] = {'quota_limited':'waiting_quota', 'running':'running',
                                'idle':'idle', 'needs_attention':'needs_attention',
                                'interrupted':'needs_attention', 'other_failure':'needs_attention'}.get(task_state, 'unknown')
                            row['statusObservedAt'] = time.time()
                        except Exception:
                            row['status'] = 'unknown'
                            row['statusObservedAt'] = time.time()
            except Exception:
                pass
        store = QueueStore(self.state_dir)
        try:
            settings = store.settings()
            settings['dispatchMinimum'] = settings['reserve'] + 10
            settings['optionalMinimum'] = settings['reserve'] + 15
            selected_auto = store.selected_auto_chats()
            settings['selectedAutoCount'] = len(selected_auto)
            manual_watches = {item['target'] for item in store.items()
                              if item['kind']=='chat' and item['enabled'] and item['origin']=='manual'}
            for row in threads:
                row['autoSelected'] = row['id'] in selected_auto or row['id'] in manual_watches
                if row['draft']:
                    row['draft']['known'] = store.known_draft(row['draft']['fingerprint'])
                    row['draft']['ignored'] = bool(store.db.execute('SELECT 1 FROM ignored_drafts WHERE fingerprint=?', (row['draft']['fingerprint'],)).fetchone())
            for draft in unbound:
                draft['known'] = store.known_draft(draft['fingerprint'])
                draft['ignored'] = bool(store.db.execute('SELECT 1 FROM ignored_drafts WHERE fingerprint=?', (draft['fingerprint'],)).fetchone())
            items = store.items()
            return {'threads':threads,'unboundDrafts':unbound,'draftsAvailable':drafts_available,
                    'draftObservedAt':draft_snapshot['observedAt'], 'draftModifiedAt':draft_snapshot['sourceModifiedAt'],
                    'inventoryError':inventory_error,'windows':windows,'settings':settings,
                    'items':items,'tasks':store.tasks(),'active':store.active(),
                    'paused':store.paused() or (not items and not settings['globalAuto'] and not selected_auto),
                    'runnerRunning':self.runner_running(),
                    'nextOffset':next_offset, 'observedAt':time.time()}
        finally:store.close()

    def _current_draft(self, thread_id, fingerprint):
        matches = [d for d in read_drafts(self.home)['drafts'] if d['threadId'] == thread_id
                   and d['fingerprint'] == fingerprint]
        if len(matches) != 1 or not matches[0]['canAdd']:
            raise RuntimeError('草稿已变化、未关联聊天或含有不能安全转发的内容')
        return matches[0]

    def _validate_thread(self, thread_id):
        with self._server() as server:
            if any(row.get('id') == thread_id for row in self._thread_list(server, True)):
                raise RuntimeError('已归档聊天不能加入队列')
            thread = server.query('thread/read', {'threadId':thread_id,'includeTurns':False}).get('thread')
        if not isinstance(thread,dict) or thread.get('id') != thread_id or thread.get('ephemeral') is not False:
            raise RuntimeError('无法确认本地聊天，未加入任务')

    def action(self, name, payload):
        if not isinstance(payload,dict):raise ValueError('参数无效')
        store = QueueStore(self.state_dir)
        def pause_new_queue_if_empty():
            if not store.items() and not store.global_auto() and not store.selected_auto_chats():
                store.set_paused(True)
        try:
            if name == 'set_auto_all':
                enabled = payload.get('enabled')
                if type(enabled) is not bool: raise ValueError('自动续跑开关无效')
                previous = store.global_auto()
                if enabled: check_version(self.app)
                store.set_global_auto(enabled)
                try:
                    if enabled: self.start_runner(store)
                except Exception:
                    store.set_global_auto(previous)
                    raise
                return {'globalAuto':enabled}
            if name == 'set_auto_chat':
                tid = str(uuid.UUID(payload.get('threadId','')))
                enabled = payload.get('enabled')
                if type(enabled) is not bool: raise ValueError('单聊天开关无效')
                old = tid in store.selected_auto_chats()
                if enabled:
                    check_version(self.app)
                    self._validate_thread(tid)
                    if store.uncertain(tid): raise RuntimeError('该聊天存在未确认发送，请先核对')
                store.set_chat_auto(tid,enabled)
                try:
                    if enabled: self.start_runner(store)
                except Exception:
                    store.set_chat_auto(tid,old)
                    raise
                if enabled:
                    with store.db:
                        watch = store.get(tid)
                        item = store.db.execute("SELECT * FROM queue_items WHERE kind='chat' AND target=?",(tid,)).fetchone()
                        if (watch and watch['status']=='paused' and item and
                                item['status'] in ('paused','queued','starting','waiting_quota','watching')):
                            store.db.execute("UPDATE watches SET enabled=1,status='starting',reason='等待重新核对' WHERE thread_id=?",(tid,))
                            store.db.execute("UPDATE queue_items SET enabled=1,status='queued',reason='',origin='auto' WHERE seq=?",(item['seq'],))
                if not enabled and not store.global_auto():
                    with store.db:
                        store.db.execute("UPDATE watches SET enabled=0,status='paused',reason='用户已关闭自动续跑' WHERE thread_id=?",(tid,))
                        store.db.execute("UPDATE queue_items SET enabled=0,status='paused',reason='用户已关闭自动续跑' WHERE kind='chat' AND target=? AND origin='manual'",(tid,))
                return {'threadId':tid,'enabled':enabled}
            if name == 'set_reserve':
                store.set_reserve(payload.get('value'))
                return {'settings':store.settings()}
            if name == 'pause':
                store.set_paused(True)
                return {'paused':True}
            if name == 'start':
                return self.start_runner(store)
            if name == 'register_inbox':
                root = Path(payload['root']).expanduser().resolve(strict=True)
                with execution_lease(store.directory) as acquired:
                    if not acquired:raise RuntimeError('调度器正在派发，请稍后再切换收件箱')
                    active = store.active()
                    if active and active['kind'] == 'inbox' and active['target'] != str(root):
                        raise RuntimeError('原收件箱仍占用执行槽，请先核对并处理')
                    pause_new_queue_if_empty()
                    store.revoke_grant()
                    store.enqueue_inbox(root)
                    with store.db:
                        store.db.execute("UPDATE queue_items SET enabled=0 WHERE kind='inbox' AND target!=?", (str(root),))
                return {'registered':True}
            if name == 'add_watch':
                tid = str(uuid.UUID(payload.get('threadId','')))
                check_version(self.app)
                assessment = inspect_task(self.home, self.app, tid)
                if not assessment['canMonitor']:
                    raise RuntimeError(assessment['reason'])
                pause_new_queue_if_empty()
                store.enqueue_chat(tid, 3)
                return {'threadId':tid}
            if name == 'open_chat':
                tid = str(uuid.UUID(payload.get('threadId','')))
                subprocess.run(['/usr/bin/open','-g','-a',str(self.app),'codex://threads/'+tid],
                               check=True,timeout=5,stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                return {'opened':True}
            if name == 'add_draft':
                draft = self._current_draft(payload.get('threadId'),payload.get('fingerprint'))
                self._validate_thread(draft['threadId'])
                pause_new_queue_if_empty()
                return {'id':store.add_task(draft['threadId'],draft['text'],'normal',draft['fingerprint'])}
            if name == 'ignore_draft':
                fp = payload.get('fingerprint')
                if not any(d['fingerprint']==fp for d in read_drafts(self.home)['drafts']):
                    raise RuntimeError('草稿已变化，请重新选择')
                store.ignore_draft(fp)
                return {'ignored':True}
            if name == 'restore_draft':
                with store.db:store.db.execute('DELETE FROM ignored_drafts WHERE fingerprint=?',(payload.get('fingerprint'),))
                return {'restored':True}
            if name == 'add_optional':
                self._validate_thread(payload.get('threadId'))
                return {'id':store.add_task(payload['threadId'],payload.get('text'),'optional')}
            if name == 'select_optional':
                store.arm_task(payload.get('id'))
                return {'selected':True}
            if name == 'reconcile_task':
                check_version(self.app)
                with execution_lease(store.directory) as acquired:
                    if not acquired:raise RuntimeError('调度器正在检查任务，请稍后核对')
                    job = store.task(payload.get('id'))
                    if job['status'] != 'uncertain' or job['turn_id'] is not None:
                        raise RuntimeError('此操作仅核对尚未确认首次发送的任务')
                    item = next((i for i in store.items() if i['kind']=='task' and i['target']==job['id']), None)
                    active = store.active()
                    if item is None or (active and active['seq'] != item['seq']):
                        raise RuntimeError('执行槽状态不匹配，保留原记录')
                    with (self.live_factory or Desktop)(self.home, self.app, read_only=True) as client:
                        state = client.snapshot(job['thread_id'])
                    if (state.get('id') != job['thread_id'] or state.get('hostId') != 'local' or
                            state.get('ephemeral') is not False or state.get('resumeState') != 'resumed' or
                            state.get('requests') != [] or state.get('threadGoal') is not None or
                            state.get('threadGoalResumeConfirmation') or state.get('queuedFollowUps') or
                            state.get('unconfirmedTurnSubmissions') or
                            (state.get('threadRuntimeStatus') or {}).get('type') != 'idle'):
                        raise RuntimeError('原聊天仍在执行、等待处理或状态未知，未解除核对')
                    turn = latest_turn(state)
                    started = turn.get('turnStartedAtMs')
                    if (turn.get('status') != 'completed' or not isinstance(turn.get('turnId'), str) or
                            not turn['turnId'] or not numeric(started) or started/1000 < job['created'] or
                            confirmed_message(turn, job['prompt']) is not True):
                        raise RuntimeError('未找到此任务创建之后内容完全匹配的已完成回合，保留待核对状态')
                    store.revoke_grant()
                    with store.db:
                        store.db.execute("UPDATE task_jobs SET status='completed',turn_id=?,reason='已只读核对原聊天完成结果',updated=? WHERE id=?", (turn['turnId'],time.time(),job['id']))
                        store.db.execute("UPDATE queue_items SET enabled=0,status='completed',reason='已核对完成结果' WHERE seq=?", (item['seq'],))
                        store.db.execute('UPDATE queue_control SET active=NULL WHERE id=1 AND active=?', (item['seq'],))
                return {'completed':True}
            if name == 'cancel_task':
                job = store.task(payload.get('id'))
                if job['status'] not in ('stored','queued','needs_attention'):
                    raise RuntimeError('已发送或不确定的任务需要先在原聊天核对')
                with execution_lease(store.directory) as acquired:
                    if not acquired:raise RuntimeError('调度器正在检查任务，请稍后再试')
                    job = store.task(job['id'])
                    if job['status'] not in ('stored','queued','needs_attention'):
                        raise RuntimeError('任务状态已改变，请重新检查')
                    active = store.active()
                    if active and active['kind']=='task' and active['target']==job['id']:
                        store.set_active(None)
                    with store.db:
                        store.db.execute("UPDATE task_jobs SET status='cancelled',updated=? WHERE id=?",(time.time(),job['id']))
                        store.db.execute("UPDATE queue_items SET enabled=0,status='cancelled',reason='用户取消' WHERE kind='task' AND target=?",(job['id'],))
                return {'cancelled':True}
            raise ValueError('未知操作')
        finally:store.close()
