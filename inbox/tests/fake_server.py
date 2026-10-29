"""Isolated JSON-RPC fixture: no credentials, network, or model calls."""
import json
import sys
import time

scenario = sys.argv[1] if len(sys.argv) > 1 else 'normal'
log_path = sys.argv[2] if len(sys.argv) > 2 else None
draft = '# 目标\n把想法保存下来。\n\n# 约束\n先做本地版本。\n\n# 下一步\n验证记录流程。'
announced_initial_account = False

def emit(value):
    line = json.dumps(value, ensure_ascii=False) + '\n'
    if scenario == 'fragmented':
        for i in range(0, len(line), 7):
            sys.stdout.write(line[i:i+7]); sys.stdout.flush()
    else:
        sys.stdout.write(line); sys.stdout.flush()

for line in sys.stdin:
    req = json.loads(line)
    if 'id' not in req:
        continue
    method = req['method']
    if log_path:
        with open(log_path, 'a') as f:
            f.write(method + '\n')
    if scenario == 'eof':
        break
    if scenario == 'timeout':
        time.sleep(5)
        continue
    if method == 'initialize':
        result = {'platformOs': 'macos'}
        if scenario == 'backpressure':
            emit({'id': req['id'], 'result': result})
            time.sleep(5)
            break
    elif method == 'account/read':
        result = {'account': {'type': 'chatgpt', 'planType': 'plus', 'email': 'fixture@example.invalid'}}
    elif method == 'model/list':
        if scenario == 'initial-account' and not announced_initial_account:
            emit({'method': 'account/updated', 'params': {'authMode': 'chatgpt'}})
            announced_initial_account = True
        result = {'data': [{'id': 'gpt-6.1-sol', 'model': 'gpt-6.1-sol',
                            'displayName': 'Sol', 'defaultReasoningEffort': 'low',
                            'supportedReasoningEfforts': [{'reasoningEffort': 'medium'}, {'reasoningEffort': 'high'}]}],
                  'nextCursor': None}
    elif method == 'account/rateLimits/read':
        result = {'accountId': 'fixture-account', 'ordinaryUsageAllowed': True,
                  'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 20, 'windowDurationMins': 300, 'resetsAt': time.time()+3600},
                                                  'secondary': {'usedPercent': 30, 'windowDurationMins': 10080, 'resetsAt': time.time()+7200},
                                                  'rateLimitReachedType': None}}}
        if scenario == 'missing-quota':
            result = {'rateLimits': None}
        if scenario == 'invalid-secondary':
            del result['rateLimitsByLimitId']['codex']['secondary']['usedPercent']
        if scenario == 'negative-quota':
            result['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = -1
        if scenario == 'unknown-bucket':
            result['rateLimitsByLimitId']['unrelated'] = result['rateLimitsByLimitId'].pop('codex')
    elif method in ('thread/start', 'thread/resume'):
        if scenario == 'quota-before-send':
            emit({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': {'usedPercent': 100}}}})
        if scenario == 'account-before-send':
            emit({'method': 'account/updated', 'params': {'authMode': None}})
        result = {'thread': {'id': 'fixture-thread', 'status': {'type': 'idle'}}}
    elif method == 'turn/start':
        if scenario == 'limited':
            emit({'id': req['id'], 'error': {'code': -32000, 'message': 'usage limit reached'}})
            continue
        if scenario == 'send-timeout':
            time.sleep(5)
            continue
        emit({'id': req['id'], 'result': {'turn': {'id': 'fixture-turn', 'status': 'inProgress'}}})
        if scenario == 'tools':
            emit({'method': 'item/commandExecution/requestApproval', 'id': 889,
                  'params': {'threadId': 'fixture-thread', 'turnId': 'fixture-turn'}})
        else:
            emit({'method': 'turn/completed', 'params': {'threadId': 'fixture-thread',
                                                       'turn': {'id': 'fixture-turn', 'status': 'completed'}}})
        continue
    elif method == 'thread/read':
        if scenario == 'read-quota' and log_path:
            with open(log_path) as f:
                first_read = f.read().splitlines().count('thread/read') == 1
            if first_read:
                emit({'id': req['id'], 'error': {'code': -32000, 'message': 'usage limit reached'}})
                continue
        result = {'thread': {'id': 'fixture-thread', 'status': {'type': 'idle'},
                             'turns': [{'id': 'fixture-turn', 'status': 'completed',
                                        'items': [{'type': 'agentMessage', 'text': draft}]}]}}
    elif method == 'turn/interrupt':
        result = {}
    else:
        emit({'id': req['id'], 'error': {'code': -32601, 'message': 'not implemented'}})
        continue
    emit({'id': req['id'], 'result': result})
