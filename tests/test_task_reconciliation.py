import sys
import unittest

if sys.platform != 'darwin':
    raise unittest.SkipTest('共享收件箱队列与原生任务管家仅支持 macOS')

import copy
import unittest
from unittest.mock import patch
import test_desktop_api as fixtures
from test_resume import state
from codex_resume.queueing import QueueStore
TID = fixtures.TID

class TaskReconciliationTests(unittest.TestCase):
    setUp = fixtures.APITests.setUp
    def make_uncertain(self):
        store=QueueStore(self.directory)
        try:
            job=store.add_task(TID,'严格匹配的测试任务','normal')
            store.update_task(job,'uncertain')
            store.set_active(store.items()[0])
            self.created=store.task(job)['created']
            return job
        finally:store.close()
    def live_state(self,text='严格匹配的测试任务',status='completed',old=False):
        result=state(status,turn_id='confirmed-turn')
        turn=result['turnHistory']['history']['entitiesByKey']['entity']
        turn.update(params={'input':[]},turnStartedAtMs=(self.created-10 if old else self.created+1)*1000,
                    items=[{'type':'userMessage','content':[{'type':'text','text':text}]}])
        owner=self
        class Live:
            def __init__(self,*a,**kw):self.read_only=kw['read_only'];owner.assertTrue(self.read_only)
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def snapshot(self,tid):owner.assertEqual(tid,TID);return copy.deepcopy(result)
        self.api.live_factory=Live
    @patch('codex_resume.desktop_api.check_version')
    def test_exact_new_completed_turn_releases_slot_without_resending(self,_version):
        job=self.make_uncertain();self.live_state()
        self.assertTrue(self.api.action('reconcile_task',{'id':job})['completed'])
        store=QueueStore(self.directory)
        try:
            self.assertEqual(store.task(job)['status'],'completed')
            self.assertIsNone(store.active())
            self.assertFalse(store.items()[0]['enabled'])
        finally:store.close()
    @patch('codex_resume.desktop_api.check_version')
    def test_old_same_text_cannot_clear_uncertainty(self,_version):
        job=self.make_uncertain();self.live_state(old=True)
        with self.assertRaises(RuntimeError):self.api.action('reconcile_task',{'id':job})
    @patch('codex_resume.desktop_api.check_version')
    def test_mismatched_or_running_turn_cannot_clear_uncertainty(self,_version):
        job=self.make_uncertain()
        for text,status in [('其他任务','completed'),('严格匹配的测试任务','inProgress')]:
            self.live_state(text,status)
            with self.assertRaises(RuntimeError):self.api.action('reconcile_task',{'id':job})
        store=QueueStore(self.directory)
        try:self.assertEqual(store.task(job)['status'],'uncertain')
        finally:store.close()
