import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from codex_resume.desktop_api import DesktopAPI
from codex_resume.queueing import QueueStore

TID='11111111-1111-4111-8111-111111111111'
PID='22222222-2222-4222-8222-222222222222'

class FakeServer:
    def __init__(self,*a,**kw):pass
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def query(self,method,params=None):
        if method=='thread/list':
            return {'data':[] if params.get('archived') else [{'id':TID,'name':'测试聊天','cwd':'/project',
                    'projectId':PID,'status':{'type':'active','activeFlags':[]}}], 'nextCursor':None}
        if method=='account/rateLimits/read':
            return {'rateLimits':{'primary':{'usedPercent':30,'windowDurationMins':300,'resetsAt':9000},'secondary':None}}
        if method=='thread/read':return {'thread':{'id':TID,'ephemeral':False}}
        raise ValueError(method)

class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.home=Path(self.tmp.name)/'home';self.home.mkdir()
        self.directory=Path(self.tmp.name)/'queue'
        self.state={'local-projects':{PID:{'id':PID,'name':'测试项目','rootPaths':['/project']}},
          'electron-persisted-atom-state':{'composer-prompt-drafts-v2':{
              'local:'+TID:{'prompt':{'document':{'type':'doc','content':[{'type':'paragraph','content':[{'type':'text','text':'先完成一项任务'}]}]}}}}}}
        (self.home/'.codex-global-state.json').write_text(json.dumps(self.state))
        self.api=DesktopAPI(self.home,'/app',self.directory,server_factory=FakeServer)
    def test_snapshot_groups_projects_and_does_not_equate_draft_to_running(self):
        snapshot=self.api.snapshot()
        row=snapshot['threads'][0]
        self.assertEqual(row['projectName'],'测试项目')
        self.assertEqual(row['status'],'running')
        self.assertEqual(row['draft']['status'],'待发送')
        self.assertEqual(snapshot['settings']['reserve'],20)
        self.assertEqual(snapshot['windows'][0]['remaining'],70)
    def test_click_add_rechecks_original_draft_and_deduplicates(self):
        draft=self.api.snapshot()['threads'][0]['draft']
        result=self.api.action('add_draft',{'threadId':TID,'fingerprint':draft['fingerprint']})
        self.assertTrue(result['id'])
        with self.assertRaises(RuntimeError):self.api.action('add_draft',{'threadId':TID,'fingerprint':draft['fingerprint']})
        self.state['electron-persisted-atom-state']['composer-prompt-drafts-v2']['local:'+TID]['prompt']['document']['content'][0]['content'][0]['text']='改过的草稿'
        (self.home/'.codex-global-state.json').write_text(json.dumps(self.state))
        with self.assertRaises(RuntimeError):self.api.action('add_draft',{'threadId':TID,'fingerprint':draft['fingerprint']})
    def test_optional_task_requires_separate_selection_and_never_sends_on_creation(self):
        job=self.api.action('add_optional',{'threadId':TID,'text':'可选的小任务'})['id']
        store=QueueStore(self.directory)
        try:
            self.assertEqual(store.task(job)['status'],'stored')
            self.assertFalse(any(item['target']==job for item in store.items()))
        finally:store.close()
        self.api.action('select_optional',{'id':job})
        store=QueueStore(self.directory)
        try:self.assertEqual(store.task(job)['status'],'queued')
        finally:store.close()
    def test_unknown_draft_cache_is_reported_unavailable(self):
        del self.state['electron-persisted-atom-state']['composer-prompt-drafts-v2']
        (self.home/'.codex-global-state.json').write_text(json.dumps(self.state))
        snapshot=self.api.snapshot()
        self.assertFalse(snapshot['draftsAvailable'])
        self.assertIsNone(snapshot['threads'][0]['draft'])
    def test_pause_and_reserve_are_persisted_without_model_dispatch(self):
        self.api.action('pause',{})
        self.api.action('set_reserve',{'value':40})
        snapshot=self.api.snapshot()
        self.assertTrue(snapshot['paused'])
        self.assertEqual(snapshot['settings']['reserve'],40)
        self.assertEqual(snapshot['settings']['dispatchMinimum'],50)
        self.assertEqual(snapshot['settings']['optionalMinimum'],55)

    def test_snapshot_reports_auto_detection_health_and_selection(self):
        store=QueueStore(self.directory)
        try:
            store.set_chat_auto(TID,True)
            store.set_auto_health('unavailable',1234)
        finally:store.close()
        settings=self.api.snapshot()['settings']
        self.assertEqual(settings['selectedAutoCount'],1)
        self.assertEqual(settings['autoHealth'],'unavailable')
        self.assertEqual(settings['autoHealthCheckedAt'],1234)

    @patch('codex_resume.desktop_api.check_version')
    def test_global_and_single_switches_start_runner_and_persist(self, version):
        with patch.object(self.api,'start_runner',return_value={'started':True}) as start:
            self.api.action('set_auto_all',{'enabled':True})
            self.assertTrue(self.api.snapshot()['settings']['globalAuto'])
            self.api.action('set_auto_all',{'enabled':False})
            self.api.action('set_auto_chat',{'threadId':TID,'enabled':True})
            snap=self.api.snapshot()
            self.assertFalse(snap['settings']['globalAuto'])
            self.assertTrue(snap['threads'][0]['autoSelected'])
            self.api.action('set_auto_chat',{'threadId':TID,'enabled':False})
            self.assertFalse(self.api.snapshot()['threads'][0]['autoSelected'])
            self.assertEqual(start.call_count,2)

    @patch('codex_resume.desktop_api.check_version')
    def test_failed_start_does_not_leave_global_auto_enabled(self, version):
        with patch.object(self.api,'start_runner',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):self.api.action('set_auto_all',{'enabled':True})
        self.assertFalse(self.api.snapshot()['settings']['globalAuto'])

    @patch('codex_resume.desktop_api.check_version')
    def test_single_switch_can_pause_and_reenable_discovered_chat(self, version):
        store=QueueStore(self.directory)
        try:
            store.set_global_auto(True);store.enroll_candidate(TID,'turn-1',1000)
            store.set_global_auto(False)
        finally:store.close()
        with patch.object(self.api,'start_runner',return_value={'started':True}):
            self.api.action('set_auto_chat',{'threadId':TID,'enabled':False})
            store=QueueStore(self.directory)
            try:self.assertFalse(store.get(TID)['enabled'])
            finally:store.close()
            self.api.action('set_auto_chat',{'threadId':TID,'enabled':True})
            store=QueueStore(self.directory)
            try:
                self.assertTrue(store.get(TID)['enabled'])
                self.assertTrue(store.chat_allowed(TID))
            finally:store.close()

    @patch('codex_resume.desktop_api.check_version')
    def test_registering_inbox_does_not_pause_enabled_auto_discovery(self, version):
        with patch.object(self.api,'start_runner',return_value={'started':True}):
            self.api.action('set_auto_all',{'enabled':True})
        root=Path(self.tmp.name)/'inbox';root.mkdir()
        self.api.action('register_inbox',{'root':str(root)})
        store=QueueStore(self.directory)
        try:self.assertFalse(store.paused())
        finally:store.close()

class RunnerStartTests(APITests):
    @patch('codex_resume.desktop_api.check_version')
    @patch('codex_resume.desktop_api.subprocess.Popen')
    def test_start_waits_for_exact_child_ready_before_unpausing(self, spawn, version):
        store=QueueStore(self.directory)
        try:store.set_auto_health('ok',1234)
        finally:store.close()
        class Child:
            pid=12345
            def poll(self):return None
        def start(command, **kwargs):
            self.assertTrue((self.directory/'queue.paused').exists())
            token=command[command.index('--ready-token')+1]
            (self.directory/'queue-runner.json').write_text(json.dumps({'pid':12345,'token':token}))
            self.assertIn('pass_fds',kwargs)
            return Child()
        spawn.side_effect=start
        result=self.api.action('start',{})
        self.assertEqual(result['pid'],12345)
        self.assertFalse((self.directory/'queue.paused').exists())
        store=QueueStore(self.directory)
        try:self.assertEqual(store.auto_health(),{'status':'unknown','checked_at':None})
        finally:store.close()
    @patch('codex_resume.desktop_api.check_version')
    @patch('codex_resume.desktop_api.subprocess.Popen')
    def test_failed_start_stays_paused(self, spawn, version):
        spawn.side_effect=OSError('failed')
        with self.assertRaises(OSError):self.api.action('start',{})
        self.assertTrue((self.directory/'queue.paused').exists())

class CommandIntegrationTests(unittest.TestCase):
    def test_real_cli_accepts_json_stdin_and_returns_no_private_trace(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as root:
            command=[sys.executable,'-m','codex_resume','--state-dir',root,'desktop','action']
            result=subprocess.run(command,input=json.dumps({'action':'set_reserve','payload':{'value':30}}),
                                  text=True,capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['settings']['reserve'],30)

class LiveStatusTests(APITests):
    def test_live_owner_state_is_separate_from_unsent_draft(self):
        class Live:
            def __init__(self,*a,**kw):self.read_only=kw.get('read_only')
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def snapshot(self,tid):
                from test_resume import state
                return dict(state('inProgress'),threadRuntimeStatus={'type':'active','activeFlags':[]})
        self.api.live_factory=Live
        result=self.api.snapshot()
        self.assertEqual(result['threads'][0]['status'],'running')
        self.assertEqual(result['threads'][0]['draft']['status'],'待发送')
        self.assertIsNotNone(result['threads'][0]['statusObservedAt'])
    def test_unavailable_owner_status_is_unknown(self):
        class Live:
            def __init__(self,*a,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def snapshot(self,tid):raise RuntimeError('not loaded')
        self.api.live_factory=Live
        result=self.api.snapshot()
        self.assertEqual(result['threads'][0]['status'],'unknown')

class RealRunnerTests(unittest.TestCase):
    def test_ready_handshake_starts_single_paused_then_unpaused_process(self):
        import os,plistlib,signal,time
        with tempfile.TemporaryDirectory() as base:
            root=Path(base);home=root/'home';home.mkdir();app=root/'ChatGPT.app';(app/'Contents').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleIdentifier':'com.openai.codex',
                'CFBundleShortVersionString':'26.1021.31345','CFBundleVersion':'21828'}))
            directory=root/'queue';api=DesktopAPI(home,app,directory,server_factory=FakeServer)
            result=api.action('start',{})
            pid=result['pid']
            try:
                self.assertTrue(api.runner_running())
                store=QueueStore(directory)
                try:self.assertFalse(store.paused())
                finally:store.close()
                api.action('pause',{})
                store=QueueStore(directory)
                try:self.assertTrue(store.paused())
                finally:store.close()
            finally:
                os.kill(pid,signal.SIGTERM)
                for _ in range(60):
                    if not api.runner_running():break
                    time.sleep(0.05)

class WatchSelectionTests(APITests):
    @patch('codex_resume.desktop_api.check_version')
    @patch('codex_resume.desktop_api.inspect_task')
    def test_user_selected_watch_is_added_but_not_started(self, inspect, version):
        inspect.return_value={'canMonitor':True,'reason':'running'}
        result=self.api.action('add_watch',{'threadId':TID})
        self.assertEqual(result['threadId'],TID)
        store=QueueStore(self.directory)
        try:
            self.assertTrue(store.paused())
            self.assertTrue(store.get(TID)['enabled'])
        finally:store.close()
    @patch('codex_resume.desktop_api.check_version')
    @patch('codex_resume.desktop_api.inspect_task')
    def test_ineligible_watch_is_never_enrolled(self, inspect, version):
        inspect.return_value={'canMonitor':False,'reason':'已完成'}
        with self.assertRaises(RuntimeError):self.api.action('add_watch',{'threadId':TID})
        store=QueueStore(self.directory)
        try:self.assertIsNone(store.get(TID))
        finally:store.close()

class QuotaFailureStatusTests(APITests):
    def test_live_quota_failed_turn_is_waiting_not_idle(self):
        class Live:
            def __init__(self,*a,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def snapshot(self,tid):
                from test_resume import state
                return dict(state('failed'),threadRuntimeStatus={'type':'systemError'})
        self.api.live_factory=Live
        result=self.api.snapshot()
        self.assertEqual(result['threads'][0]['status'],'waiting_quota')

class InboxSwitchTests(APITests):
    def test_switch_disables_previous_inbox(self):
        first=self.home/'one'; second=self.home/'two'; first.mkdir(); second.mkdir()
        self.api.action('register_inbox',{'root':str(first)})
        self.api.action('register_inbox',{'root':str(second)})
        store=QueueStore(self.directory)
        try:
            enabled=[i['target'] for i in store.items() if i['kind']=='inbox' and i['enabled']]
            self.assertEqual(enabled,[str(second.resolve())])
        finally:store.close()
