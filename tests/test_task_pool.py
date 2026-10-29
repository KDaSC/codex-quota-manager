import copy
import tempfile
import unittest
from pathlib import Path
from codex_resume.queueing import QueueStore, Scheduler
from test_resume import state, quota, THREAD

OTHER='22222222-2222-4222-8222-222222222222'

class TaskPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=QueueStore(Path(self.tmp.name)/'queue');self.addCleanup(self.store.close)
        self.states={THREAD:state('completed'),OTHER:dict(state('completed'),id=OTHER)}
        self.sent=[];self.now=1000;self.payload=dict(quota(10,10),_accountKey='account-a')
        self.drafts=[];owner=self
        class Desktop:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def snapshot(self,tid):return copy.deepcopy(owner.states[tid])
            def submit(self,tid,text,expected,message_id,dispatch_guard=None):
                if not dispatch_guard():raise RuntimeError('cancelled')
                owner.sent.append((tid,text))
                if getattr(owner,'uncertain',False):raise TimeoutError()
                owner.states[tid]=dict(state('inProgress',turn_id='new-'+str(len(owner.sent))),id=tid,
                                       threadRuntimeStatus={'type':'active','activeFlags':[]})
                return 'new-'+str(len(owner.sent))
        self.desktop=Desktop
        self.scheduler=Scheduler(self.store,Desktop,lambda:self.payload,clock=lambda:self.now,drafts_reader=lambda:self.drafts)
    def add(self,thread,text,kind,fp=None):return self.store.add_task(thread,text,kind,fp)
    def test_optional_waits_until_managed_normal_task_completed(self):
        optional=self.add(OTHER,'可选小任务','optional');self.store.arm_task(optional)
        normal=self.add(THREAD,'正常任务','normal')
        self.scheduler.tick();self.assertEqual(self.sent,[(THREAD,'正常任务')])
        self.scheduler.tick();self.assertEqual(len(self.sent),1)
        self.states[THREAD]=state('completed',turn_id='new-1')
        self.scheduler.tick();self.scheduler.tick()
        self.assertEqual(self.sent,[(THREAD,'正常任务'),(OTHER,'可选小任务')])
        self.assertEqual(self.store.task(normal)['status'],'completed')
    def test_optional_requires_both_windows_at_least_floor_plus_15(self):
        optional=self.add(THREAD,'可选任务','optional');self.store.arm_task(optional)
        self.payload=dict(quota(66,10),_accountKey='account-a') # 34% vs required 35%
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.payload=dict(quota(65,10),_accountKey='account-a') # exactly 35%
        self.scheduler.tick();self.assertEqual(self.sent,[(THREAD,'可选任务')])
    def test_optional_does_not_start_if_normal_draft_awaits_user_selection(self):
        optional=self.add(THREAD,'可选任务','optional');self.store.arm_task(optional)
        self.drafts=[{'threadId':OTHER,'fingerprint':'draft-fp','canAdd':True}]
        self.scheduler.tick();self.assertEqual(self.sent,[])
    def test_changed_original_draft_requires_reconfirmation(self):
        self.add(THREAD,'已确认的原文','normal','old-fp')
        self.drafts=[{'threadId':THREAD,'fingerprint':'new-fp','canAdd':True}]
        self.scheduler.tick();self.assertEqual(self.sent,[])
        self.assertEqual(self.store.tasks()[0]['status'],'needs_attention')
    def test_uncertain_dispatch_is_not_duplicated_after_restart(self):
        self.add(THREAD,'请处理','normal');self.uncertain=True
        self.scheduler.tick();self.assertEqual(len(self.sent),1)
        again=Scheduler(self.store,self.desktop,lambda:self.payload,clock=lambda:self.now,drafts_reader=lambda:self.drafts)
        again.tick();self.assertEqual(len(self.sent),1)
        self.assertEqual(self.store.tasks()[0]['status'],'uncertain')
    def test_pending_approval_never_starts_new_task(self):
        self.add(THREAD,'请处理','normal');self.states[THREAD]['requests']=[{'method':'approval'}]
        self.scheduler.tick();self.assertEqual(self.sent,[])
    def test_two_jobs_for_same_running_chat_count_as_one_active_chat(self):
        self.add(THREAD,'第一条','normal');self.add(THREAD,'第二条','normal')
        self.scheduler.tick()
        self.assertEqual(self.scheduler.tick(),'running')
        self.assertEqual(self.sent,[(THREAD,'第一条')])

    def test_optional_guard_rechecks_new_draft_immediately_before_send(self):
        self.add(THREAD,'选做任务','optional')
        task_id=self.store.tasks()[0]['id'];self.store.arm_task(task_id)
        reads=0
        def draft_reader():
            nonlocal reads
            reads+=1
            return [] if reads==1 else [{'fingerprint':'new-draft','threadId':OTHER,'canAdd':True}]
        scheduler=Scheduler(self.store,self.desktop,lambda:self.payload,clock=lambda:self.now,drafts_reader=draft_reader)
        scheduler.tick()
        self.assertEqual(self.sent,[])
    def test_draft_is_rechecked_at_dispatch_guard(self):
        self.add(THREAD,'原文','normal','old-fp')
        reads = 0
        def drafts():
            nonlocal reads
            reads += 1
            return [{'threadId':THREAD,'fingerprint':'old-fp' if reads == 1 else 'new-fp'}]
        scheduler=Scheduler(self.store,self.desktop,lambda:self.payload,clock=lambda:self.now,drafts_reader=drafts)
        scheduler.tick()
        self.assertEqual(self.sent,[])
    def test_archived_chat_cannot_receive_task(self):
        self.add(THREAD,'原文','normal')
        scheduler=Scheduler(self.store,self.desktop,lambda:self.payload,clock=lambda:self.now,
                            drafts_reader=lambda:[],archived_reader=lambda tid:True)
        scheduler.tick()
        self.assertEqual(self.sent,[])
    def test_empty_new_inbox_does_not_block_optional_tasks(self):
        root=Path(self.tmp.name)/'inbox';root.mkdir()
        self.store.enqueue_inbox(root)
        optional=self.add(THREAD,'选做','optional');self.store.arm_task(optional)
        self.scheduler.tick()
        self.assertEqual(self.sent,[(THREAD,'选做')])
    def test_waiting_task_detects_changed_chat_after_scheduler_restart(self):
        job=self.add(THREAD,'任务','normal')
        self.scheduler.tick()
        self.states[THREAD]=state('failed',turn_id='new-1')
        self.payload=dict(quota(90,10),_accountKey='account-a')
        self.scheduler.tick()
        self.assertEqual(self.store.task(job)['status'],'waiting_quota')
        self.states[THREAD]['latestModel']='changed-model'
        self.payload=dict(quota(10,10),_accountKey='account-a')
        again=Scheduler(self.store,self.desktop,lambda:self.payload,clock=lambda:self.now,drafts_reader=lambda:self.drafts)
        again.tick()
        self.assertEqual(self.store.task(job)['status'],'needs_attention')
        self.assertEqual(len(self.sent),1)
    def test_unopened_optional_chat_does_not_block_normal_task(self):
        optional=self.add(OTHER,'空闲再做','optional');self.store.arm_task(optional)
        self.add(THREAD,'先做重要的','normal')
        parent=self.desktop
        class OptionalUnavailable(parent):
            def snapshot(self,tid):
                if tid==OTHER:raise RuntimeError('optional chat not open')
                return super().snapshot(tid)
        scheduler=Scheduler(self.store,OptionalUnavailable,lambda:self.payload,clock=lambda:self.now,drafts_reader=lambda:self.drafts)
        scheduler.tick()
        self.assertEqual(self.sent,[(THREAD,'先做重要的')])
