"""Install or remove the guarded queue runner as this user's login service.

launchd owns process lifetime only. Neither installing nor uninstalling clears
queue.paused, enables global management, changes individual selection, or
deletes dispatch records.
"""
import argparse
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import uuid


LABEL_PREFIX = 'local.'
DEFAULT_LABEL = 'local.codex-resume.runner'
ALLOWED_CHARS = set('abcdefghijklmnopqrstuvwxyz0123456789.-')


def validate_label(label):
    """Only accept this tool's own local launchd label namespace."""
    if not label.startswith(LABEL_PREFIX) or any(c not in ALLOWED_CHARS for c in label):
        raise ValueError('后台服务名称无效：必须以 local. 开头，且只含小写字母、数字、点和连字符')
    if 'codex-resume' not in label:
        raise ValueError('后台服务名称必须包含 codex-resume，避免接管无关服务')
    return label


def discover_labels(agents):
    """Existing labels this tool may adopt, so upgrades do not install a duplicate."""
    if not agents.is_dir():
        return []
    found = []
    for path in sorted(agents.glob('*.plist')):
        name = path.stem
        try:
            validate_label(name)
        except ValueError:
            continue
        found.append(name)
    return found


def uninstall(label, agents):
    """Stop and remove one service. Never touches queue state or dispatch records."""
    validate_label(label)
    target = 'gui/' + str(os.getuid())
    # A missing job makes bootout fail; that is not an error for uninstall.
    subprocess.run(['/bin/launchctl', 'bootout', target + '/' + label],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    path = agents / (label + '.plist')
    if path.is_symlink():
        raise RuntimeError('拒绝删除符号链接服务配置')
    if path.exists():
        path.unlink()
    return path


def main():
    parser = argparse.ArgumentParser(description='为已安装续航配置登录后恢复后台，或卸载该后台')
    parser.add_argument('--bundle', type=Path, default=Path('/Applications/Codex续航.app'))
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.codex-auto-resume')
    parser.add_argument('--home', type=Path, default=Path.home()/'.codex')
    parser.add_argument('--app', type=Path, default=Path('/Applications/ChatGPT.app'))
    parser.add_argument('--label', default=None,
                        help='launchd 服务名；默认沿用已存在的服务名，否则使用 %s' % DEFAULT_LABEL)
    parser.add_argument('--uninstall', action='store_true', help='停止并移除该后台服务')
    args = parser.parse_args()

    agents = Path.home()/'Library/LaunchAgents'
    if args.uninstall:
        label = args.label
        if label is None:
            existing = discover_labels(agents)
            if not existing:
                print('未发现本工具登记的后台服务，无需卸载')
                return 0
            label = existing[0]
        path = uninstall(label, agents)
        print('已停止并移除后台服务：%s' % label)
        print('队列状态、人工暂停和发送记录均未修改。')
        if path.exists():
            raise RuntimeError('服务配置文件仍然存在')
        return 0

    label = args.label
    if label is None:
        existing = discover_labels(agents)
        # Adopt an existing label so re-running the installer upgrades in place.
        label = existing[0] if existing else DEFAULT_LABEL
    validate_label(label)

    backend = args.bundle.resolve(strict=True)/'Contents/Resources/backend'
    if not (backend/'codex_resume/__main__.py').is_file():
        raise RuntimeError('安装的应用缺少续跑核心')
    state = args.state_dir.expanduser().absolute()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = state.lstat()
    if state.is_symlink() or not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise RuntimeError('状态目录必须是当前用户的私有目录')
    for name in ('service.log','service-error.log'):
        fd = os.open(state/name, os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW, 0o600)
        os.close(fd)
    command = ['/usr/bin/python3','-u','-m','codex_resume',
               '--home',str(args.home.expanduser().absolute()),'--app',str(args.app.expanduser().absolute()),
               '--state-dir',str(state),'queue','run','--ready-token',str(uuid.uuid4())]
    config = {'Label':label,'ProgramArguments':command,'WorkingDirectory':str(backend),
              'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':60,'ProcessType':'Interactive',
              'LimitLoadToSessionType':'Aqua','StandardOutPath':str(state/'service.log'),
              'StandardErrorPath':str(state/'service-error.log')}
    agents.mkdir(parents=True, exist_ok=True)
    destination = agents/(label+'.plist')
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():raise RuntimeError('拒绝覆盖符号链接服务配置')
        old = plistlib.loads(destination.read_bytes())
        if old.get('WorkingDirectory') != str(backend) or old.get('ProgramArguments',[])[:-1] != command[:-1]:
            raise RuntimeError('已存在不同的后台配置，停止覆盖')
        if old.get('ProcessType') != 'Interactive':
            # This helper supplies the app's live status and timed dispatch.
            # Background throttling caused real read-only RPC timeouts. Keep
            # the existing identity and all other settings when upgrading.
            old['ProcessType'] = 'Interactive'
            temporary = destination.with_suffix('.plist.next')
            fd = os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'wb') as stream:plistlib.dump(old,stream)
            os.replace(temporary,destination)
    else:
        fd = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:plistlib.dump(config,stream)
    target = 'gui/'+str(os.getuid())
    present = subprocess.run(['/bin/launchctl','print',target+'/'+label],
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode == 0
    if not present:
        subprocess.run(['/bin/launchctl','bootstrap',target,str(destination)],check=True)
    print(destination)
    print('后台服务已登记；人工暂停、开关、版本校验及发送去重继续由原队列执行。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
