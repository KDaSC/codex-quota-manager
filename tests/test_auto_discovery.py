"""Auto-discovery selects only the latest structured quota failures."""
import sys
import unittest

if sys.platform != 'darwin':
    raise unittest.SkipTest('共享收件箱队列与原生任务管家仅支持 macOS')

import unittest
from unittest.mock import Mock
from pathlib import Path
import tempfile

from test_resume import THREAD

OTHER = '22222222-2222-4222-8222-222222222222'
CHILD = '33333333-3333-4333-8333-333333333333'


class AutoDiscoveryTests(unittest.TestCase):
    def test_global_discovery_reads_only_latest_turn_and_excludes_children(self):
        from codex_resume.tasks import discover_quota_candidates
        server = Mock()
        def query(method, params):
            if method == 'thread/list':
                self.assertFalse(params['archived'])
                self.assertNotIn('subAgent', params['sourceKinds'])
                return {'data': [
                    {'id':THREAD,'ephemeral':False,'parentThreadId':None},
                    {'id':OTHER,'ephemeral':False,'parentThreadId':None},
                    {'id':CHILD,'ephemeral':False,'parentThreadId':THREAD}], 'nextCursor':None}
            self.assertEqual(method,'thread/turns/list')
            self.assertEqual(params['limit'],1)
            self.assertEqual(params['itemsView'],'notLoaded')
            turn={'id':'quota-turn','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}
            if params['threadId']==OTHER:turn={'id':'done','status':'completed','error':None}
            return {'data':[turn], 'nextCursor':None}
        server.query.side_effect=query
        self.assertEqual(discover_quota_candidates(server),[(THREAD,'quota-turn')])

    def test_selection_limits_inspection_and_unknown_response_fails_closed(self):
        from codex_resume.tasks import discover_quota_candidates
        server=Mock()
        server.query.side_effect=[{'data':[{'id':THREAD,'ephemeral':False},
                                          {'id':OTHER,'ephemeral':False}], 'nextCursor':None},
                                  {'data':[{'id':'x','status':'failed','error':{'message':'quota'}}], 'nextCursor':None}]
        self.assertEqual(discover_quota_candidates(server,selected={THREAD}),[])
        self.assertEqual(server.query.call_count,2)


class AutoEnrollmentTests(unittest.TestCase):
    def setUp(self):
        from codex_resume.queueing import QueueStore
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=QueueStore(Path(self.tmp.name)/'queue');self.addCleanup(self.store.close)

    def test_global_and_individual_authorization_persist_and_never_duplicate(self):
        self.assertFalse(self.store.auto_authorized(THREAD))
        self.assertFalse(self.store.enroll_candidate(THREAD,'turn-1',1000))
        self.store.set_global_auto(True)
        self.assertTrue(self.store.enroll_candidate(THREAD,'turn-1',1000))
        self.assertFalse(self.store.enroll_candidate(THREAD,'turn-1',1001))
        self.assertEqual(len(self.store.items()),1)
        self.assertEqual(self.store.items()[0]['origin'],'auto')
        self.assertEqual(self.store.quota_wait(THREAD,'turn-1'),1060)
        self.store.set_global_auto(False)
        self.assertFalse(self.store.chat_allowed(THREAD))
        self.store.set_chat_auto(THREAD,True)
        self.assertTrue(self.store.chat_allowed(THREAD))
        self.store.set_chat_auto(THREAD,False)
        self.assertFalse(self.store.chat_allowed(THREAD))

    def test_explicit_pause_is_not_undone_by_global_discovery(self):
        self.store.set_global_auto(True)
        self.assertTrue(self.store.enroll_candidate(THREAD,'turn-1',1000))
        item=self.store.items()[0]
        self.store.remove(item['seq'])
        self.assertFalse(self.store.enroll_candidate(THREAD,'turn-1',2000))
        self.assertFalse(self.store.enroll_candidate(THREAD,'turn-2',2000))
        self.assertFalse(self.store.get(THREAD)['enabled'])

    def test_new_quota_failure_clears_only_previous_auto_round_baseline(self):
        self.store.set_global_auto(True)
        self.store.enroll_candidate(THREAD,'turn-1',1000)
        item=self.store.items()[0]
        self.store.update_item(item,'waiting_quota',baseline='previous-round')
        self.assertTrue(self.store.enroll_candidate(THREAD,'turn-2',2000))
        self.assertIsNone(self.store.items()[0]['baseline'])
        self.assertEqual(self.store.quota_wait(THREAD,'turn-2'),2060)
        self.store.update_item(item,'checking',baseline='current-round')
        self.assertFalse(self.store.enroll_candidate(THREAD,'turn-2',2001))
        self.assertEqual(self.store.items()[0]['baseline'],'current-round')

    def test_scanner_enrolls_only_authorized_candidates_and_throttles(self):
        from contextlib import nullcontext
        from codex_resume.queueing import AutoScanner
        self.store.set_chat_auto(THREAD,True)
        clock=[1000];calls=[]
        def discover(server,selected):
            calls.append(selected)
            return [(THREAD,'turn-1'),(OTHER,'turn-2')]
        scanner=AutoScanner(self.store,lambda:nullcontext(object()),lambda:{'_accountKey':'a'},
                            clock=lambda:clock[0], discover=discover)
        self.assertEqual(scanner.poll(),'checked')
        self.assertEqual(calls,[{THREAD}])
        self.assertEqual([i['target'] for i in self.store.items()],[THREAD])
        scanner.poll();self.assertEqual(len(calls),1)
        clock[0]=1060;scanner.poll();self.assertEqual(len(calls),2)

    def test_scanner_never_enrolls_after_global_is_turned_off(self):
        from contextlib import nullcontext
        from codex_resume.queueing import AutoScanner
        self.store.set_global_auto(True)
        def discover(server,selected):
            self.store.set_global_auto(False)
            return [(THREAD,'turn-1')]
        scanner=AutoScanner(self.store,lambda:nullcontext(object()),lambda:{'_accountKey':'a'},
                            clock=lambda:1000,discover=discover)
        scanner.poll()
        self.assertEqual(self.store.items(),[])

    def test_scanner_failure_is_visible_until_a_successful_recheck(self):
        from contextlib import nullcontext
        from codex_resume.queueing import AutoScanner
        self.store.set_global_auto(True);clock=[1000]
        calls=[0]
        def discover(server,selected):
            calls[0]+=1
            if calls[0]==1:raise RuntimeError('simulated connection failure')
            return []
        scanner=AutoScanner(self.store,lambda:nullcontext(object()),lambda:{'_accountKey':'a'},
                            clock=lambda:clock[0],discover=discover)
        with self.assertRaises(RuntimeError):scanner.poll()
        self.assertEqual(self.store.auto_health()['status'],'unavailable')
        self.assertEqual(scanner.poll(),'waiting')
        self.assertEqual(self.store.auto_health()['status'],'unavailable')
        clock[0]=1060;self.assertEqual(scanner.poll(),'checked')
        self.assertEqual(self.store.auto_health()['status'],'ok')


class AutoSwitchSchedulerTests(unittest.TestCase):
    from test_queue import QueueTests
    setUp=QueueTests.setUp

    def test_global_off_blocks_discovered_chat_until_individually_enabled(self):
        self.store.set_global_auto(True)
        self.assertTrue(self.store.enroll_candidate(THREAD,'t1',1000))
        self.store.set_global_auto(False);self.now=1100
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.store.set_chat_auto(THREAD,True)
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])

    def test_new_failed_turn_while_waiting_rebinds_live_settings_before_resuming(self):
        from test_resume import state,quota
        self.store.set_global_auto(True);self.store.enroll_candidate(THREAD,'t1',1000)
        self.now=1060;self.quota=dict(quota(100),_accountKey='account-a')
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.states[THREAD]=state(turn_id='turn-2')
        self.states[THREAD]['latestModel']='user-selected-new-model'
        self.assertTrue(self.store.enroll_candidate(THREAD,'turn-2',2000))
        self.now=2000;self.quota=dict(quota(reset=4000),_accountKey='account-a')
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.now=2060;self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])

    def test_switch_off_during_final_check_prevents_send(self):
        from codex_resume.queueing import Scheduler
        self.store.set_global_auto(True);self.store.enroll_candidate(THREAD,'t1',1000);self.now=1100
        owner=self;original=self.desktop
        class TurnOffBeforeSend(original):
            def resume(self,tid,baseline,message_id,dispatch_guard=None):
                owner.store.set_global_auto(False)
                return super().resume(tid,baseline,message_id,dispatch_guard=dispatch_guard)
        scheduler=Scheduler(self.store,TurnOffBeforeSend,lambda:self.quota,clock=lambda:self.now)
        scheduler.tick()
        self.assertEqual(self.sent,[])

    def test_global_off_waits_for_running_turn_then_allows_individually_selected_chat(self):
        from test_resume import state
        self.store.set_global_auto(True);self.store.enroll_candidate(THREAD,'t1',1000);self.now=1100
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
        self.store.set_global_auto(False)
        self.store.set_chat_auto(self.other,True)
        self.assertTrue(self.store.enroll_candidate(self.other,'t1',1100))
        self.now=1160
        self.scheduler.tick();self.assertEqual(self.sent,[THREAD])
        self.assertEqual(self.store.active()['target'],THREAD)
        self.states[THREAD]=state('completed',turn_id='new')
        self.scheduler.tick()
        self.assertEqual(self.sent,[THREAD,self.other])

    def test_global_off_does_not_release_active_chat_when_state_is_unknown(self):
        from codex_resume.app import ThreadUnavailable
        from codex_resume.queueing import Scheduler
        self.store.set_global_auto(True);self.store.enroll_candidate(THREAD,'t1',1000);self.now=1100
        self.scheduler.tick();self.store.set_global_auto(False)
        class Unavailable(self.desktop):
            def snapshot(self,tid):raise ThreadUnavailable('unloaded')
        scheduler=Scheduler(self.store,Unavailable,lambda:self.quota,clock=lambda:self.now)
        self.assertEqual(scheduler.tick(),'needs_attention')
        self.assertEqual(self.store.active()['target'],THREAD)

    def test_global_off_does_not_release_uncertain_send(self):
        from codex_resume.queueing import Scheduler
        self.store.set_global_auto(True);self.store.enroll_candidate(THREAD,'t1',1000);self.now=1100
        self.lose_ack=True;self.scheduler.tick()
        self.assertEqual(self.sent,[THREAD])
        self.store.set_global_auto(False);self.store.set_chat_auto(self.other,True)
        self.store.enroll_candidate(self.other,'t1',1100);self.now=1160
        restarted=Scheduler(self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        self.assertEqual(restarted.tick(),'uncertain')
        self.assertEqual(self.sent,[THREAD])
        self.assertEqual(self.store.active()['target'],THREAD)

    def test_full_simulated_early_reset_restarts_without_duplicate(self):
        from contextlib import nullcontext
        from codex_resume.queueing import AutoScanner, Scheduler
        from test_resume import quota
        self.store.set_global_auto(True)
        self.quota=dict(quota(10,100),_accountKey='account-a')
        scanner=AutoScanner(self.store,lambda:nullcontext(object()),lambda:self.quota,
                            clock=lambda:self.now,discover=lambda server,selected:[(THREAD,'t1')])
        self.assertEqual(scanner.poll(),'checked')
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.now=1060;self.scheduler.tick();self.assertEqual(self.sent,[])
        self.quota=dict(quota(10,10,reset=10000,weekly_reset=20000),_accountKey='account-a')
        self.now=4660;self.scheduler.tick();self.assertEqual(self.sent,[])
        restarted=Scheduler(self.store,self.desktop,lambda:self.quota,clock=lambda:self.now)
        self.now=4719;restarted.tick();self.assertEqual(self.sent,[])
        self.now=4720;restarted.tick();self.assertEqual(self.sent,[THREAD])
        restarted.tick();self.assertEqual(self.sent,[THREAD])
        self.assertEqual(self.store.count(THREAD),1)

