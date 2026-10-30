"""Persistent queue behavior; desktop transport is simulated, disk/locks are real."""
import sys
import unittest

if sys.platform != 'darwin':
    raise unittest.SkipTest('共享收件箱队列与原生任务管家仅支持 macOS')

import copy
import json
from pathlib import Path
import tempfile
import unittest

from codex_resume.policy import quota_status
from codex_resume.store import Store
from test_resume import state, quota, THREAD


class ReserveTests(unittest.TestCase):
    def test_managed_quota_requires_actual_five_hour_and_weekly_windows(self):
        q=quota(10,10)
        q['rateLimitsByLimitId']['codex']['secondary']['windowDurationMins']=300
        self.assertFalse(quota_status(q,1000,require_secondary=True)[0])

    def test_both_windows_keep_ten_percent(self):
        for a,b in [(90,10),(10,90),(100,100)]:
            ready,when,_=quota_status(quota(a,b),1000)
            self.assertFalse(ready)
            self.assertEqual(when,8060 if b>=90 else 2060)
        self.assertTrue(quota_status(quota(89,89),1000)[0])

    def test_current_macos_has_exact_profile_only(self):
        import plistlib
        from codex_resume.app import check_version, AppError
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'Contents';p.mkdir()
            info={'CFBundleIdentifier':'com.openai.codex','CFBundleShortVersionString':'26.1021.31345','CFBundleVersion':'21828'}
            (p/'Info.plist').write_bytes(plistlib.dumps(info))
            self.assertFalse(check_version(root).get('diagnosticOnly',False))
            info['CFBundleVersion']='21829'
            (p/'Info.plist').write_bytes(plistlib.dumps(info))
            with self.assertRaises(AppError):check_version(root)

class QueueTests(unittest.TestCase):
    def setUp(self):
        from codex_resume.queueing import QueueStore, Scheduler
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=QueueStore(Path(self.tmp.name)/'queue');self.addCleanup(self.store.close)
        self.other='22222222-2222-4222-8222-222222222222'
        self.states={THREAD:state(),self.other:dict(state(),id=self.other)}
        self.sent=[];self.now=1000;self.quota=quota();self.quota['_accountKey']='account-a'
        owner=self
        class Desktop:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def snapshot(self,tid):return copy.deepcopy(owner.states[tid])
            def resume(self,tid,baseline,message_id,dispatch_guard=None):
                if not dispatch_guard():raise RuntimeError('cancelled')
                owner.sent.append(tid)
                if getattr(owner,'lose_ack',False):raise TimeoutError()
                owner.states[tid]=dict(state('inProgress',turn_id='new'),id=tid,threadRuntimeStatus={'type':'active','activeFlags':[]})
                return 'new'
        self.desktop=Desktop
        self.scheduler=Scheduler(self.store,Desktop,lambda:self.quota,clock=lambda:self.now)
    def add(self,tid=THREAD):self.store.enqueue_chat(tid,3)
    def test_fifo_waits_for_first_completion_before_second_send(self):
        self.add();self.add(self.other)
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(self.sent,[THREAD])
        self.states[THREAD]=state('completed',turn_id='new')
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(self.sent,[THREAD,self.other])
    def test_reserve_then_actual_reset_not_clock_alone(self):
        self.add();self.quota= dict(quota(90,100),_accountKey='account-a')
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.now=9000;self.scheduler.tick();self.assertEqual(self.sent,[])
        self.quota=dict(quota(10,10,reset=10000,weekly_reset=20000),_accountKey='account-a')
        self.now=9100;self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
    def test_pause_before_dispatch_and_after_restart(self):
        self.add();self.store.set_paused(True);self.scheduler.tick()
        self.assertEqual(self.sent,[])
        self.assertTrue((self.store.directory/'queue.paused').exists())
        self.store.set_paused(False);self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
    def test_uncertain_dispatch_blocks_entire_queue_after_restart(self):
        from codex_resume.queueing import Scheduler
        self.add();self.add(self.other);self.lose_ack=True;self.scheduler.tick()
        again=Scheduler(self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        again.tick();self.assertEqual(self.sent,[THREAD])
        self.assertEqual(self.store.active()['target'],THREAD)
    def test_existing_active_selected_chat_blocks_other_dispatch(self):
        self.add();self.add(self.other)
        self.states[self.other]=dict(state('inProgress'),id=self.other,threadRuntimeStatus={'type':'active','activeFlags':[]})
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.assertEqual(self.store.active()['target'],self.other)
    def test_account_change_and_unknown_state_block(self):
        self.add();self.store.set_paused(True);self.scheduler.tick();self.store.set_paused(False)
        self.quota= dict(quota(90),_accountKey='account-a');self.scheduler.tick()
        self.quota= dict(quota(),_accountKey='account-b');self.now=3000
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.assertTrue(self.store.paused())
    def test_duplicate_enrollment_does_not_reset_attempts_or_order(self):
        self.add();self.add(self.other);self.scheduler.tick();self.add()
        self.assertEqual(len(self.store.items()),2)
        self.assertEqual(self.store.count(THREAD),1)
        self.assertEqual(self.store.items()[0]['target'],THREAD)
    def test_approval_and_manual_interruption_stop_only_selected_job(self):
        self.add();self.add(self.other);self.states[THREAD]['requests']=[{'method':'approval'}]
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(self.sent,[self.other])
        self.assertFalse(self.store.items()[0]['enabled'])
    def test_inbox_grants_only_automatic_pending_task_and_waits_for_readback(self):
        root=Path(self.tmp.name)/'inbox';root.mkdir()
        def save(status='queued',auto=True):
            (root/'state.json').write_text(json.dumps({'schemaVersion':1,'stopped':False,'tasks':[{'id':'idea-1','automatic':auto,'status':status}]}))
        save(auto=False);self.store.enqueue_inbox(root);self.add()
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
        self.states[THREAD]=state('completed',turn_id='new');self.scheduler.tick()
        save();self.scheduler.tick()
        grant=json.loads((self.store.directory/'inbox-grant.json').read_text())
        self.assertEqual(grant['root'],str(root.resolve()));self.assertEqual(grant['taskID'],'idea-1')
        save('completed');self.scheduler.tick();self.assertIsNone(self.store.active())
    def test_inbox_uncertainty_and_pause_fail_closed(self):
        root=Path(self.tmp.name)/'inbox';root.mkdir()
        (root/'state.json').write_text(json.dumps({'schemaVersion':1,'stopped':False,'tasks':[{'id':'idea','automatic':True,'status':'uncertain'}]}))
        self.store.enqueue_inbox(root);self.add();self.scheduler.tick()
        self.assertEqual(self.sent,[])
    def test_global_execution_lock_excludes_another_dispatcher(self):
        from codex_resume.queueing import execution_lease
        self.add()
        with execution_lease(self.store.directory) as acquired:
            self.assertTrue(acquired);self.scheduler.tick()
        self.assertEqual(self.sent,[])
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])

    def test_unloaded_selected_chat_requests_reopen_without_sending(self):
        from codex_resume.app import ThreadUnavailable
        from codex_resume.queueing import Scheduler
        self.add()
        calls=[]
        class Unloaded(self.desktop):
            def snapshot(self,tid): raise ThreadUnavailable('not loaded')
        scheduler=Scheduler(self.store,Unloaded,lambda:self.quota,clock=lambda:self.now,
                            reopen_chat=lambda tid: calls.append(tid))
        self.assertEqual(scheduler.tick(),'waiting_connection')
        self.assertEqual(calls,[THREAD]);self.assertEqual(self.sent,[])
        scheduler.tick();self.assertEqual(calls,[THREAD])

    def test_unloaded_chat_timeout_requests_reopen_without_sending(self):
        from codex_resume.queueing import Scheduler
        self.add();calls=[]
        class TimedOut(self.desktop):
            def snapshot(self,tid): raise TimeoutError('snapshot unavailable')
        scheduler=Scheduler(self.store,TimedOut,lambda:self.quota,clock=lambda:self.now,
                            reopen_chat=lambda tid: calls.append(tid))
        self.assertEqual(scheduler.tick(),'waiting_connection')
        self.assertEqual(calls,[THREAD]);self.assertEqual(self.sent,[])

class QueueCLITests(unittest.TestCase):
    def test_queue_add_status_pause_remove_without_background_launch(self):
        import contextlib,io
        from unittest.mock import patch
        from codex_resume.__main__ import main
        with tempfile.TemporaryDirectory() as root:
            def cli(*args):
                out=io.StringIO()
                with contextlib.redirect_stdout(out):main(['--state-dir',root,'queue',*args])
                return json.loads(out.getvalue())
            with patch('codex_resume.__main__.check_version'),patch('codex_resume.__main__.inspect_task',return_value={'canMonitor':True}),patch('subprocess.Popen') as spawn:
                cli('add',THREAD)
                status=cli('status')
                self.assertEqual(status['items'][0]['target'],THREAD)
                self.assertTrue(status['paused']) # Enrollment alone never starts work.
                cli('pause');self.assertTrue(cli('status')['paused'])
                cli('remove','1');self.assertFalse(cli('status')['items'][0]['enabled'])
                spawn.assert_not_called()

class RestartSafetyTests(unittest.TestCase):
    setUp = QueueTests.setUp
    add = QueueTests.add
    def test_reset_delay_survives_scheduler_restart(self):
        from codex_resume.queueing import Scheduler
        self.add()
        self.quota = dict(quota(100, 10), _accountKey='account-a')
        self.scheduler.tick()
        self.now = 2020
        self.quota = dict(quota(10, 10, reset=10000, weekly_reset=20000), _accountKey='account-a')
        restarted = Scheduler(self.store, self.desktop, lambda:self.quota, clock=lambda:self.now)
        restarted.tick()
        self.assertEqual(self.sent, [])
        self.store.set_reserve(10)
        restarted.tick()
        self.assertEqual(self.sent, [])
        self.now = 2060
        restarted.tick()
        self.assertEqual(self.sent, [THREAD])

    def test_early_reset_detected_hourly_and_waits_one_minute_after_detection(self):
        from codex_resume.queueing import Scheduler
        self.add()
        self.quota = dict(quota(10, 100), _accountKey='account-a')
        self.scheduler.tick()
        self.quota = dict(quota(10, 10, reset=10000, weekly_reset=20000), _accountKey='account-a')
        self.now = 4599; self.scheduler.tick(); self.assertEqual(self.sent, [])
        self.now = 4600; self.scheduler.tick(); self.assertEqual(self.sent, [])
        restarted = Scheduler(self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        self.now = 4659; restarted.tick(); self.assertEqual(self.sent, [])
        self.now = 4660; restarted.tick(); self.assertEqual(self.sent, [THREAD])

    def test_hourly_check_does_not_authorize_missing_second_window(self):
        self.add()
        self.quota = dict(quota(10, 100), _accountKey='account-a')
        self.scheduler.tick()
        self.now = 4600
        self.quota = dict(quota(10,10,reset=10000,weekly_reset=20000),_accountKey='account-a')
        self.quota['rateLimitsByLimitId']['codex']['secondary'] = None
        self.scheduler.tick(); self.assertEqual(self.sent, [])

    def test_waiting_fingerprint_survives_new_scheduler(self):
        from codex_resume.queueing import Scheduler
        self.add();self.quota=dict(quota(100),_accountKey='account-a');self.scheduler.tick()
        self.states[THREAD]['cwd']='/changed-workspace'
        self.quota=dict(quota(),_accountKey='account-a')
        again=Scheduler(self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        self.assertEqual(again.tick(),'changed');self.assertEqual(self.sent,[])

    def test_explicit_reselection_after_change_uses_new_baseline(self):
        self.add();self.quota=dict(quota(100),_accountKey='account-a');self.scheduler.tick()
        self.states[THREAD]['cwd']='/changed-workspace'
        self.quota=dict(quota(),_accountKey='account-a')
        self.assertEqual(self.scheduler.tick(),'changed');self.assertEqual(self.sent,[])
        self.store.enqueue_chat(THREAD,3)
        self.assertIsNone(self.store.items()[0]['baseline'])
        self.now=2100;self.quota=dict(quota(reset=4000),_accountKey='account-a')
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
    def test_legacy_controller_cannot_bypass_managed_queue_or_global_pause(self):
        from codex_resume.controller import Controller
        self.add()
        controller=Controller(THREAD,self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        controller.step();self.assertEqual(self.sent,[])
        self.store.set_paused(True);self.scheduler.tick();self.assertEqual(self.sent,[])

class CurrentMacProtocolTests(unittest.TestCase):
    def test_22943_accepts_only_tested_cli_and_uses_official_queue(self):
        import plistlib
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from codex_resume.app import check_version, mac_uses_queue, AppError
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'Contents';p.mkdir()
            (p/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.openai.codex',
                'CFBundleShortVersionString':'26.1027.30824','CFBundleVersion':'22943'}))
            with patch('codex_resume.app.subprocess.run',return_value=CompletedProcess([],0,'codex-cli 0.166.0-alpha.5.1\n','')):
                info=check_version(root,'macos')
                self.assertTrue(mac_uses_queue(info))
            with patch('codex_resume.app.subprocess.run',return_value=CompletedProcess([],0,'codex-cli 0.0.0\n','')):
                with self.assertRaises(AppError):check_version(root,'macos')

    def test_verified_22391_uses_official_queue_and_exact_cli_pair(self):
        import plistlib
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from codex_resume.app import check_version, AppError, Desktop, CONTINUATION
        from codex_resume.policy import fingerprint
        from test_windows_compatibility import ScriptedPipe
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'Contents';p.mkdir()
            (p/'Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.openai.codex',
                'CFBundleShortVersionString':'26.1023.50603','CFBundleVersion':'22391'}))
            with patch('codex_resume.app.subprocess.run',return_value=CompletedProcess([],0,'codex-cli 0.165.0-alpha.1.2\n','')):
                info=check_version(root,'macos')
            with patch('codex_resume.app.subprocess.run',return_value=CompletedProcess([],0,'codex-cli 0.0.0\n','')):
                with self.assertRaises(AppError):check_version(root,'macos')
        transport=ScriptedPipe();transport.state['ephemeral']=False
        app=Desktop('/unused',system='macos');app.app_info=info;app.sock=transport
        def send(command,**kwargs):
            turn=transport.state['turnHistory']['history']['entitiesByKey']['tail']
            turn.update(turnId='queue-new-turn',status='inProgress',params={'input':[{'type':'text','text':CONTINUATION,'text_elements':[]}]})
            return CompletedProcess(command,0,'Queued message 33333333-3333-4333-8333-333333333333 for thread '+THREAD+'.\n','')
        with patch('codex_resume.app.check_version',return_value=info),patch('codex_resume.app.subprocess.run',side_effect=send) as run:
            current=app.snapshot(THREAD)
            self.assertEqual(app.resume(THREAD,fingerprint(current),'local-intent'),'queue-new-turn')
        self.assertEqual(run.call_count,1)
        self.assertFalse(any(m.get('method')=='thread-follower-start-turn' for m in transport.messages))

    def test_turn_confirmation_uses_single_user_message_if_input_cache_is_empty(self):
        from codex_resume.app import confirmed_message
        turn={'params':{'input':[]},'items':[{'type':'userMessage','content':[{'type':'text','text':'测试','text_elements':[]}]}]}
        self.assertTrue(confirmed_message(turn,'测试'))
        self.assertFalse(confirmed_message(turn,'不同内容'))
        turn['items'].append({'type':'userMessage','content':[{'type':'text','text':'测试'}]})
        self.assertFalse(confirmed_message(turn,'测试'))
        self.assertIsNone(confirmed_message({'params':{'input':[]},'items':[]},'测试'))

    def test_current_build_uses_official_queue_and_confirms_new_turn(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from test_windows_compatibility import ScriptedPipe
        from codex_resume.app import Desktop,CONTINUATION
        from codex_resume.policy import fingerprint
        transport=ScriptedPipe();transport.state['ephemeral']=False
        app=Desktop('/unused',system='macos');app.app_info={'platform':'macos','version':'26.1021.31345','build':'21828'};app.sock=transport
        calls=[]
        def send(command,**kwargs):
            calls.append(command)
            turn=transport.state['turnHistory']['history']['entitiesByKey']['tail']
            turn.update(turnId='queue-new-turn',status='inProgress',params={'input':[{'type':'text','text':CONTINUATION,'text_elements':[]}]})
            return CompletedProcess(command,0,'Queued message 33333333-3333-4333-8333-333333333333 for thread '+THREAD+'.\n','')
        with patch('codex_resume.app.check_version',return_value=app.app_info),patch('codex_resume.app.subprocess.run',side_effect=send):
            current=app.snapshot(THREAD)
            self.assertEqual(app.resume(THREAD,fingerprint(current),'local-intent'),'queue-new-turn')
        self.assertEqual(calls[0][1:],['queue','--thread',THREAD,'--message',CONTINUATION])
        self.assertEqual(len(calls),1)
        self.assertFalse(any(m.get('method')=='thread-follower-start-turn' for m in transport.messages))

class InboxOperationTests(unittest.TestCase):
    setUp = QueueTests.setUp
    add = QueueTests.add
    def test_needs_input_with_unsettled_operation_holds_slot(self):
        root=Path(self.tmp.name)/'inbox';root.mkdir()
        (root/'state.json').write_text(json.dumps({'schemaVersion':1,'stopped':False,'tasks':[{'id':'idea','automatic':True,'status':'needsInput','operation':{'phase':'uncertain'}}]}))
        self.store.enqueue_inbox(root);self.add()
        self.scheduler.tick();self.assertEqual(self.sent,[])

class AccountQuotaTests(unittest.TestCase):
    def test_ordinary_usage_denial_never_authorizes_credits(self):
        payload=quota();payload['ordinaryUsageAllowed']=False
        self.assertFalse(quota_status(payload,1000)[0])

class PreflightRaceTests(unittest.TestCase):
    setUp=QueueTests.setUp
    add=QueueTests.add
    def test_quota_is_read_again_immediately_before_dispatch(self):
        self.add(); original=self.desktop; owner=self
        class QuotaDropsBeforeSend(original):
            def resume(self,tid,baseline,message_id,dispatch_guard=None):
                owner.quota=dict(quota(100,10),_accountKey='account-a')
                return super().resume(tid,baseline,message_id,dispatch_guard=dispatch_guard)
        from codex_resume.queueing import Scheduler
        scheduler=Scheduler(self.store,QuotaDropsBeforeSend,lambda:self.quota,clock=lambda:self.now)
        scheduler.tick()
        self.assertEqual(self.sent,[])
        self.assertTrue(self.store.uncertain(THREAD))

    def test_change_between_queue_scan_and_controller_check_never_sends(self):
        self.add();owner=self;original=self.desktop
        class ChangesOnSecondRead(original):
            reads=0
            def snapshot(self,tid):
                type(self).reads+=1
                if type(self).reads==2:owner.states[tid]['cwd']='/user-changed-workspace'
                return super().snapshot(tid)
        from codex_resume.queueing import Scheduler
        scheduler=Scheduler(self.store,ChangesOnSecondRead,lambda:self.quota,clock=lambda:self.now)
        scheduler.tick();self.assertEqual(self.sent,[])
