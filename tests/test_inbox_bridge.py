"""Real Python -> Swift CLI -> simulated App Server integration, no cloud model."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from codex_resume.queueing import QueueStore, Scheduler

ROOT=Path(__file__).resolve().parents[1]
BINARY=ROOT/'inbox/.build/debug/idea-inbox'

class NativeBridgeTests(unittest.TestCase):
    @unittest.skipUnless(BINARY.exists(), 'build native inbox CLI for cross-language acceptance')
    def test_grant_one_native_turn_pause_and_exactly_once_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);data=base/'data';log=base/'rpc.log';bindir=base/'bin';bindir.mkdir()
            wrapper=bindir/'codex';fixture=ROOT/'inbox/tests/fake_server.py'
            wrapper.write_text(f'#!{sys.executable}\nimport runpy,sys\nsys.argv=[{str(fixture)!r},"normal",{str(log)!r}]\nrunpy.run_path({str(fixture)!r},run_name="__main__")\n')
            wrapper.chmod(0o700)
            env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ.get('PATH',''))
            def cli(*args):
                result=subprocess.run([str(BINARY),'--root',str(data),*args],env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)
                self.last_stderr=result.stderr
                return json.loads(result.stdout)
            task=cli('capture','--text','合成测试，无个人资料','--auto')
            ledger=QueueStore(base/'queue')
            try:
                ledger.enqueue_inbox(data)
                cli('work');self.assertFalse(log.exists()) # No permit -> zero RPCs.
                payload={'_accountKey':hashlib.sha256(b'fixture-account').hexdigest(),'rateLimits':{'primary':{'usedPercent':10,'windowDurationMins':300,'resetsAt':time.time()+3600},'secondary':{'usedPercent':10,'windowDurationMins':10080,'resetsAt':time.time()+7200}}}
                scheduler=Scheduler(ledger,None,lambda:payload)
                self.assertEqual(scheduler.tick(),'inbox_granted')
                ledger.set_paused(True);cli('work');self.assertFalse(log.exists())
                ledger.set_paused(False);scheduler.tick()
                result=cli('work')
                self.assertEqual(result['tasks'][0]['status'],'completed', json.dumps({'result':result,'grant':json.loads((ledger.directory/'inbox-grant.json').read_text()),'rpc':log.read_text() if log.exists() else None,'diagnostic':self.last_stderr}))
                draft=Path(result['tasks'][0]['draftPath']);self.assertTrue(draft.is_file())
                self.assertIn('目标',draft.read_text())
                cli('work')
                self.assertEqual(log.read_text().splitlines().count('turn/start'),1)
                self.assertEqual(scheduler.tick(),'settled')
                self.assertIsNone(ledger.active())
            finally:ledger.close()
