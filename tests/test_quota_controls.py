import sys
import unittest

if sys.platform != 'darwin':
    raise unittest.SkipTest('共享收件箱队列与原生任务管家仅支持 macOS')

import tempfile
import unittest
from codex_resume.policy import quota_status
from codex_resume.queueing import QueueStore, Scheduler
from test_resume import quota, state, THREAD

class QuotaControlsTests(unittest.TestCase):
    def test_selected_floor_adds_ten_points_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store=QueueStore(root)
            self.assertEqual(store.settings()['reserve'],20)
            store.set_reserve(30)
            self.assertEqual(store.dispatch_minimum(),40)
            store.close()
            store=QueueStore(root)
            self.assertEqual(store.settings()['reserve'],30)
            for invalid in (0,15,50,True,'20'):
                with self.assertRaises(ValueError):store.set_reserve(invalid)
            store.close()

    def test_all_windows_must_exceed_the_selected_dispatch_line(self):
        # 20% final reserve + 10% closeout buffer => stop at 30% remaining.
        self.assertFalse(quota_status(quota(70,10),1000,minimum_remaining=30)[0])
        self.assertFalse(quota_status(quota(10,70),1000,minimum_remaining=30)[0])
        self.assertTrue(quota_status(quota(69,69),1000,minimum_remaining=30)[0])

    def test_optional_starts_at_equal_to_reserve_plus_fifteen(self):
        self.assertTrue(quota_status(quota(65,10),1000,minimum_remaining=35,allow_equal=True)[0])
        self.assertFalse(quota_status(quota(66,10),1000,minimum_remaining=35,allow_equal=True)[0])
        self.assertFalse(quota_status(quota(10,66),1000,minimum_remaining=35,allow_equal=True)[0])

    def test_managed_queue_does_not_dispatch_if_weekly_window_disappears(self):
        from test_task_pool import TaskPoolTests
        case=TaskPoolTests('test_optional_requires_both_windows_at_least_floor_plus_15');case.setUp()
        try:
            case.add(THREAD,'测试','normal')
            case.payload['rateLimitsByLimitId']['codex']['secondary']=None
            case.scheduler.tick()
            self.assertEqual(case.sent,[])
        finally:case.doCleanups()

    def test_changing_reserve_controls_real_scheduler_and_persisted_grant(self):
        from test_queue import QueueTests
        case=QueueTests('test_fifo_waits_for_first_completion_before_second_send')
        case.setUp()
        try:
            case.add();case.store.set_reserve(30) # 40% threshold
            case.quota=dict(quota(61,10),_accountKey='account-a')
            self.assertEqual(case.scheduler.tick(),'waiting_quota')
            self.assertEqual(case.sent,[])
            case.store.set_reserve(20) # now 30%, previous cached wait must be invalidated
            case.scheduler.tick()
            self.assertEqual(case.sent,[THREAD])
        finally:
            case.doCleanups()

class DraftSnapshotTests(unittest.TestCase):
    def test_local_drafts_are_separate_from_running_and_preserve_paragraphs(self):
        from codex_resume.drafts import parse_drafts
        state={'electron-persisted-atom-state':{'composer-prompt-drafts-v2':{
            'local:11111111-1111-4111-8111-111111111111':{'prompt':{'document':{'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'第一行'}]},{'type':'paragraph','content':[{'type':'text','text':'第二行'}]}]}}},
            'local:22222222-2222-4222-8222-222222222222':{'prompt':{'document':{'type':'doc','content':[{'type':'paragraph'}]}}},
            'client-new-thread:abc':{'prompt':{'document':{'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'未绑定'}]}]}}}
        }}}
        data=parse_drafts(state)
        self.assertEqual(data[0]['threadId'],'11111111-1111-4111-8111-111111111111')
        self.assertEqual(data[0]['text'],'第一行\n第二行')
        self.assertEqual(data[0]['status'],'待发送')
        self.assertEqual(len(data),2) # one empty draft is omitted; unbound is visible.
        self.assertIsNone(data[1]['threadId'])
    def test_rich_drafts_are_detected_but_cannot_be_silently_sent_as_text_only(self):
        from codex_resume.drafts import parse_drafts
        state={'electron-persisted-atom-state':{'composer-prompt-drafts-v2':{
            'local:11111111-1111-4111-8111-111111111111':{'prompt':{'document':{'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'查看'}, {'type':'image','attrs':{'path':'private'}}]}]}}}
        }}}
        d=parse_drafts(state)[0]
        self.assertTrue(d['hasUnsupportedContent'])
        self.assertFalse(d['canAdd'])
    def test_unknown_or_unreadable_drafts_are_not_reported_empty(self):
        from codex_resume.drafts import parse_drafts, DraftUnavailable
        for state in ({}, {'electron-persisted-atom-state':{'composer-prompt-drafts-v2':[]}},
                      {'electron-persisted-atom-state':{'composer-prompt-drafts-v2':{'local:x':{'prompt':{'document':{}}}}}}):
            with self.assertRaises(DraftUnavailable):parse_drafts(state)
