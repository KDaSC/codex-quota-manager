"""Guard checks for the optional login-service helper; never touches launchd."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


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
            (agents / 'local.codex-resume.runner.plist').write_text('x')
            (agents / 'com.apple.unrelated.plist').write_text('x')
            (agents / 'local.other-tool.plist').write_text('x')
            self.assertEqual(self.module.discover_labels(agents),
                             ['local.codex-resume.runner'])


if __name__ == '__main__':
    unittest.main()
