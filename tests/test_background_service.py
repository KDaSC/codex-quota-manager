"""Guard checks for the optional login-service helper; never touches launchd."""
import importlib.util
import plistlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock


ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        'install_background', ROOT / 'scripts/install_background.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BackgroundLabelTests(unittest.TestCase):
    def setUp(self):
        self.module = load()

    def test_accepts_neutral_and_legacy_labels(self):
        for label in ('local.codex-resume.runner',
                      'local.liudongkun.codex-resume.runner'):
            self.assertEqual(self.module.validate_label(label), label)

    def test_rejects_foreign_or_unsafe_labels(self):
        for label in ('com.apple.codex-resume', 'local.something',
                      'LOCAL.codex-resume', 'local.codex-resume;rm -rf /',
                      'local.codex-resume/../x', '../codex-resume'):
            with self.assertRaises(ValueError):
                self.module.validate_label(label)

    def test_discovers_only_own_services(self):
        with tempfile.TemporaryDirectory() as folder:
            agents = Path(folder)
            self.make_owned(agents, 'local.codex-resume.runner')
            (agents / 'com.apple.unrelated.plist').write_text('x')
            (agents / 'local.other-tool.plist').write_text('x')
            (agents / 'local.other-codex-resume.plist').write_text('foreign')
            self.assertEqual(self.module.discover_labels(agents),
                             ['local.codex-resume.runner'])

    def make_owned(self, agents, label):
        p=agents/(label+'.plist')
        p.write_bytes(plistlib.dumps({'Label':label,
          'WorkingDirectory':'/Applications/Codex续航.app/Contents/Resources/backend',
          'ProgramArguments':['/usr/bin/python3','-u','-m','codex_resume',
             '--home','/tmp/home','--app','/tmp/app','--state-dir','/tmp/state',
             'queue','run','--ready-token','11111111-1111-4111-8111-111111111111']}))
        return p

    def test_uninstall_does_not_stop_or_remove_a_foreign_matching_name(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder);label='local.other-codex-resume'
            p=agents/(label+'.plist');p.write_text('not our service')
            with patch.object(self.module.subprocess,'run') as launchctl:
                with self.assertRaises((ValueError,RuntimeError)):self.module.uninstall(label,agents)
                launchctl.assert_not_called()
            self.assertTrue(p.exists())

    def test_uninstall_retains_configuration_if_stop_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder);label='local.codex-resume.runner';p=self.make_owned(agents,label)
            with patch.object(self.module.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=5)]):
                with self.assertRaises(RuntimeError):self.module.uninstall(label,agents)
            self.assertTrue(p.exists())

    def test_uninstall_removes_owned_configuration_when_service_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder);label='local.codex-resume.runner';p=self.make_owned(agents,label)
            with patch.object(self.module.subprocess,'run',return_value=Mock(returncode=113)):
                self.module.uninstall(label,agents)
            self.assertFalse(p.exists())

    def test_uninstall_confirms_running_service_is_gone_before_removing(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder);label='local.codex-resume.runner';p=self.make_owned(agents,label)
            with patch.object(self.module.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=0),Mock(returncode=113)]):
                self.module.uninstall(label,agents)
            self.assertFalse(p.exists())

    def test_uninstall_keeps_configuration_if_service_still_listed(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder);label='local.codex-resume.runner';p=self.make_owned(agents,label)
            with patch.object(self.module.subprocess,'run',side_effect=[Mock(returncode=0),Mock(returncode=0),Mock(returncode=0)]):
                with self.assertRaises(RuntimeError):self.module.uninstall(label,agents)
            self.assertTrue(p.exists())

    def test_multiple_owned_services_require_explicit_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            agents=Path(folder)
            self.make_owned(agents,'local.codex-resume.runner')
            self.make_owned(agents,'local.liudongkun.codex-resume.runner')
            with self.assertRaises(RuntimeError):self.module.select_existing_label(agents)


if __name__ == '__main__':
    unittest.main()
