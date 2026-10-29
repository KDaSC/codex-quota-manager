import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
BINARY = PROJECT / '.build/debug/idea-inbox'


class CLIFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='idea-inbox-test-')
        self.root = Path(self.tmp.name) / 'data'

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, success=True):
        self.assertTrue(BINARY.exists(), '尚未实现可运行的 idea-inbox 命令')
        p = subprocess.run([str(BINARY), '--root', str(self.root), *args],
                           capture_output=True, text=True, timeout=8)
        if success:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0)
        return p

    def test_capture_round_trip_without_ai(self):
        text = '现在的想法：离线也能记录\n保留原文、空格和表情 🙂  '
        record = self.cli('capture', '--text', text)
        self.assertEqual(record['text'], text)
        self.assertEqual(record['status'], 'recorded')
        self.assertEqual(self.cli('show', record['id'])['text'], text)

    def test_duplicate_content_gets_separate_ids(self):
        a = self.cli('capture', '--text', '同一个想法')
        b = self.cli('capture', '--text', '同一个想法')
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(len(self.cli('list')['tasks']), 2)

    def test_automatic_mode_and_stop_survive_restart(self):
        a = self.cli('capture', '--text', '整理一份草稿', '--auto')
        self.assertEqual(a['status'], 'queued')
        self.cli('stop')
        self.assertTrue(self.cli('list')['stopped'])
        self.cli('allow')
        self.assertFalse(self.cli('list')['stopped'])
        self.cli('cancel', a['id'])
        self.assertEqual(self.cli('show', a['id'])['status'], 'cancelled')

    def test_blank_text_rejected(self):
        self.cli('capture', '--text', ' \n\t', success=False)
        self.assertFalse((self.root / 'state.json').exists())

    def test_corrupted_state_not_overwritten(self):
        self.cli('capture', '--text', '已有原文')
        bad = b'{corrupted'
        (self.root / 'state.json').write_bytes(bad)
        self.cli('capture', '--text', '不要覆盖', success=False)
        self.assertEqual((self.root / 'state.json').read_bytes(), bad)
        self.assertTrue((self.root / 'state.backup.json').exists())

    def test_unknown_version_not_overwritten(self):
        self.cli('capture', '--text', '已有原文')
        p = self.root / 'state.json'
        value = json.loads(p.read_text())
        value['schemaVersion'] = 99
        p.write_text(json.dumps(value))
        before = p.read_bytes()
        self.cli('capture', '--text', '新原文', success=False)
        self.assertEqual(p.read_bytes(), before)

    def test_second_writer_rejected(self):
        self.root.mkdir()
        with (self.root / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cli('capture', '--text', '锁冲突', success=False)
            self.assertFalse((self.root / 'state.json').exists())

    def test_unwritable_root_reports_failure(self):
        self.root.write_text('这是文件而不是目录')
        self.cli('capture', '--text', '不能显示成功', success=False)
        self.assertEqual(self.root.read_text(), '这是文件而不是目录')

    def test_state_is_private_and_backup_readable(self):
        a = self.cli('capture', '--text', '第一条')
        self.cli('capture', '--text', '第二条')
        self.assertEqual((self.root / 'state.json').stat().st_mode & 0o077, 0)
        backup = json.loads((self.root / 'state.backup.json').read_text())
        self.assertTrue(any(t['id'] == a['id'] for t in backup['tasks']))


if __name__ == '__main__':
    unittest.main()
