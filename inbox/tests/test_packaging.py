import importlib.util
import subprocess
from pathlib import Path
import tempfile
import unittest
import plistlib


class Packaging(unittest.TestCase):
    def test_build_snapshot_contains_sources_but_not_runtime_records(self):
        spec = importlib.util.spec_from_file_location('package_script', Path(__file__).parents[1] / 'scripts/build_app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'project'; root.mkdir()
            (root/'Package.swift').write_text('manifest')
            (root/'Sources').mkdir();(root/'Sources/App.swift').write_text('source')
            (root/'state.json').write_text('private record')
            out=module.prepare_build_sources(root,Path(folder)/'snapshot')
            self.assertEqual((out/'Sources/App.swift').read_text(),'source')
            self.assertFalse((out/'state.json').exists())

    def test_generated_bundle_strips_signing_detritus_only(self):
        spec = importlib.util.spec_from_file_location('package_script', Path(__file__).parents[1] / 'scripts/build_app.py')
        module = importlib.util.module_from_spec(spec)
        # Loading the build helper must not build or sign the application.
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / 'test.app'
            app.mkdir()
            subprocess.run(['xattr', '-wx', 'com.apple.FinderInfo', '00' * 32, str(app)], check=True)
            subprocess.run(['xattr', '-w', 'org.idea-inbox.fixture', 'preserve', str(app)], check=True)
            module.remove_signing_detritus(app)
            self.assertNotIn('com.apple.FinderInfo', subprocess.check_output(['xattr', str(app)], text=True).splitlines())
            self.assertEqual(subprocess.check_output(['xattr', '-p', 'org.idea-inbox.fixture', str(app)], text=True).strip(), 'preserve')

    def test_portable_bundle_does_not_keep_build_machine_data_path(self):
        spec = importlib.util.spec_from_file_location('package_script', Path(__file__).parents[1] / 'scripts/build_app.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / '想法收件箱.app'
            contents = app / 'Contents'
            contents.mkdir(parents=True)
            with (contents / 'Info.plist').open('wb') as stream:
                plistlib.dump({'CFBundleName': '想法收件箱', 'IdeaInboxDataRoot': '/Users/build/私有项目/运行数据'}, stream)
            portable = Path(folder) / 'portable.app'
            module.prepare_portable_bundle(app, portable)
            with (portable / 'Contents/Info.plist').open('rb') as stream:
                info = plistlib.load(stream)
            with (app / 'Contents/Info.plist').open('rb') as stream:
                development_info = plistlib.load(stream)
            self.assertNotIn('IdeaInboxDataRoot', info)
            self.assertEqual(development_info['IdeaInboxDataRoot'], '/Users/build/私有项目/运行数据')
