"""Read Codex's persisted composer drafts; never writes back to Codex storage.

The key is an observed desktop format, not a public stable API. Unknown shapes
fail closed and must be reported as unavailable rather than empty.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid


class DraftUnavailable(RuntimeError):
    pass


def _document_text(document):
    if not isinstance(document, dict) or document.get('type') != 'doc' or not isinstance(document.get('content'), list):
        raise DraftUnavailable('草稿文档结构无法识别')
    unsupported = False
    def render(node):
        nonlocal unsupported
        if not isinstance(node, dict) or not isinstance(node.get('type'), str):
            raise DraftUnavailable('草稿节点结构无法识别')
        kind = node['type']
        if kind == 'text':
            if not isinstance(node.get('text'), str):
                raise DraftUnavailable('草稿文本格式无法识别')
            return node['text']
        if kind == 'hardBreak':
            return '\n'
        if kind not in ('doc', 'paragraph'):
            unsupported = True
        children = node.get('content', [])
        if not isinstance(children, list):
            raise DraftUnavailable('草稿内容结构无法识别')
        separator = '\n' if kind == 'doc' else ''
        return separator.join(render(child) for child in children)
    return render(document), unsupported


def parse_drafts(state):
    try:
        drafts = state['electron-persisted-atom-state']['composer-prompt-drafts-v2']
    except (KeyError, TypeError):
        raise DraftUnavailable('当前 Codex 草稿缓存不可用')
    if not isinstance(drafts, dict):
        raise DraftUnavailable('当前 Codex 草稿缓存格式不受支持')
    result = []
    for key, item in drafts.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise DraftUnavailable('草稿条目格式不受支持')
        prompt = item.get('prompt')
        if not isinstance(prompt, dict):
            raise DraftUnavailable('草稿内容不可读取')
        text, unsupported = _document_text(prompt.get('document'))
        if not text.strip() and not unsupported:
            continue
        unsupported = unsupported or bool(item.get('pullRequestChecks'))
        thread_id = None
        if key.startswith('local:'):
            try:
                thread_id = str(uuid.UUID(key.removeprefix('local:')))
            except ValueError as exc:
                raise DraftUnavailable('本地草稿缺少有效聊天标识') from exc
        canonical = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        result.append({'key':key, 'threadId':thread_id, 'text':text, 'status':'待发送',
                       'hasUnsupportedContent':unsupported,
                       'canAdd': bool(thread_id and text.strip() and not unsupported and len(text.encode()) <= 12000),
                       'fingerprint':hashlib.sha256(canonical).hexdigest()})
    return result


def read_drafts(home):
    path = Path(home) / '.codex-global-state.json'
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 32 * 1024 * 1024:
                raise DraftUnavailable('Codex 草稿文件权限或大小不受支持')
            state = json.load(file)
    except (OSError, ValueError, TypeError) as exc:
        raise DraftUnavailable('无法读取本机 Codex 草稿缓存') from exc
    return {'drafts':parse_drafts(state), 'observedAt':time.time(), 'sourceModifiedAt':info.st_mtime}
