"""Build a local app bundle, without installation or account changes."""
from pathlib import Path
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile


def project_version(root):
    """Read the version from the Python package so the two cannot drift."""
    source = (root.parent / 'codex_resume/__init__.py').read_text(encoding='utf-8')
    match = re.search(r"__version__\s*=\s*'([^']+)'", source)
    if not match:
        raise RuntimeError('无法从 codex_resume/__init__.py 读取版本号')
    return match.group(1)


def prepare_portable_bundle(source, destination):
    """Copy the development bundle without its machine-specific data location."""
    shutil.copytree(source, destination)
    info_path = destination / 'Contents/Info.plist'
    with info_path.open('rb') as stream:
        info = plistlib.load(stream)
    info.pop('IdeaInboxDataRoot', None)
    with info_path.open('wb') as stream:
        plistlib.dump(info, stream)


def prepare_build_sources(root, destination):
    """Build immutable local copies of source, never the inbox's runtime data."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    shutil.copy2(root / 'Package.swift', destination / 'Package.swift')
    for name in ('Sources', 'Assets', 'tests'):
        if (root / name).is_dir():
            shutil.copytree(root / name, destination / name)
    return destination

def remove_signing_detritus(app):
    for path in [app, *app.rglob('*')]:
        for name in ('com.apple.FinderInfo', 'com.apple.ResourceFork'):
            attributes = subprocess.check_output(['xattr', str(path)], text=True).splitlines()
            if name in attributes:
                subprocess.run(['xattr', '-d', name, str(path)], check=True)


def main():
    root = Path(__file__).resolve().parents[1]
    version = project_version(root)
    if not all((root / 'Assets' / name).is_file() for name in ('AppIcon.icns', 'app-icon.png')):
        subprocess.run(['swift', str(root / 'scripts/generate_icon.swift')], cwd=root, check=True)
    # Keep module caches and the build database out of File Provider storage.
    scratch = Path.home() / 'Library/Caches/CodexTaskManagerBuild-v4'
    source = prepare_build_sources(root, scratch / 'package')
    build = ['swift', 'build', '--scratch-path', str(scratch / 'build')]
    build_env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET='15.0')
    subprocess.run([*build, '-j', '1', '-c', 'release', '--product', 'idea-inbox-app'], cwd=source, check=True, env=build_env)
    binary_dir = subprocess.check_output([*build, '-c', 'release', '--show-bin-path'], cwd=source, text=True, env=build_env).strip()
    # File Provider re-adds FinderInfo to .app directories in this project.
    # Keep the executable bundle in a local cache and a metadata-free zip here.
    app = Path.home() / ('Library/Caches/CodexTaskManagerDevelopment/Codex任务管家-%s.app' % version)
    macos = app / 'Contents/MacOS'
    macos.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(binary_dir) / 'idea-inbox-app', macos / 'IdeaInbox')
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True, exist_ok=True)
    for name in ('AppIcon.icns', 'app-icon.png'):
        shutil.copy2(root / 'Assets' / name, resources / name)
    backend = app / 'Contents/Resources/backend/codex_resume'
    if backend.exists():shutil.rmtree(backend)
    backend.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root.parent / 'codex_resume', backend,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.log'))
    info = {'CFBundleName': 'Codex任务管家', 'CFBundleDisplayName': 'Codex任务管家',
            'CFBundleIdentifier': os.environ.get('CODEX_TASK_MANAGER_BUNDLE_ID',
                                                'local.liudongkun.codex-task-manager'),
            'CFBundleExecutable': 'IdeaInbox',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '6', 'CFBundleShortVersionString': version,
            'CFBundleIconFile': 'AppIcon.icns',
            'LSMinimumSystemVersion': '15.0', 'NSHighResolutionCapable': True}
    with (app / 'Contents/Info.plist').open('wb') as f:
        plistlib.dump(info, f)
    remove_signing_detritus(app)
    subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True, capture_output=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True, capture_output=True)
    archive = app.parent / ('Codex任务管家-%s.zip' % version)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='idea-inbox-portable-') as folder:
        portable = Path(folder) / app.name
        prepare_portable_bundle(app, portable)
        subprocess.run(['codesign', '--force', '--sign', '-', str(portable)], check=True, capture_output=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(portable)], check=True, capture_output=True)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in portable.rglob('*'):
                if path.is_file():
                    bundle.write(path, Path(app.name) / path.relative_to(portable))
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError('应用压缩包校验失败')
    project_archive = root / ('应用/Codex任务管家-%s.zip' % version)
    project_archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, project_archive)
    print(app)
    print(archive)
    print(project_archive)


if __name__ == '__main__':
    main()
