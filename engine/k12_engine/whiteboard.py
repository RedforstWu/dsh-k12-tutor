"""Bounded teaching scripts. The cursor counts steps admitted for rendering, not speech."""
from __future__ import annotations

import json
import hashlib
import math
import re
import time
import uuid

from . import guard
from .session import Workspace, current_task

ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class BoardError(ValueError):
    pass


def save(ws, doc):
    path = ws.root / 'boards' / (doc['boardId'] + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def stop(doc):
    doc['generation'] += 1
    doc['revision'] += 1
    doc['run'] = None


def invalidate(ws):
    for path in (ws.root / 'boards').glob('*.json'):
        doc = json.loads(path.read_text(encoding='utf-8'))
        if doc['sessionId'] == ws.session or (doc['run'] and doc['run'].get('sessionId') == ws.session):
            stop(doc)
            save(ws, doc)


def validate(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise BoardError('steps must contain 1..12 steps')
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'draw', 'say', 'holdMs'}:
            raise BoardError('step requires only draw, say, holdMs')
        if not isinstance(step['say'], str) or len(step['say']) > 1000:
            raise BoardError('say must be at most 1000 characters')
        if type(step['holdMs']) is not int or not 0 <= step['holdMs'] <= 10000:
            raise BoardError('holdMs must be 0..10000')
        if not isinstance(step['draw'], list) or not 1 <= len(step['draw']) <= 40:
            raise BoardError('draw must contain 1..40 operations')
        for shape in step['draw']:
            if not isinstance(shape, dict) or not ID.fullmatch(str(shape.get('id', ''))):
                raise BoardError('invalid shape id')
            kind = shape.get('kind')
            fields = {'remove': set(), 'text': {'x', 'y', 'text'}, 'rect': {'x', 'y', 'w', 'h'},
                      'ellipse': {'x', 'y', 'w', 'h'}, 'line': {'x', 'y', 'dx', 'dy'},
                      'arrow': {'x', 'y', 'dx', 'dy'}}
            if kind not in fields or set(shape) != fields[kind] | {'id', 'kind'}:
                raise BoardError('invalid shape fields')
            for field in fields[kind] - {'text'}:
                value = shape[field]
                if type(value) not in (float, int) or not math.isfinite(value) or not -1600 <= value <= 1600:
                    raise BoardError('coordinates must be finite and within 1600')
                if field in ('w', 'h') and value <= 0:
                    raise BoardError('size must be positive')
            if kind == 'text' and (not isinstance(shape['text'], str) or not 1 <= len(shape['text']) <= 1000):
                raise BoardError('text must contain 1..1000 characters')


def policy(task, steps):
    if task is None or task.get('answer_policy') == 'refuse':
        raise BoardError('board policy refuses this task')
    if task.get('answer_policy') != 'withhold':
        return
    texts = [s['say'] for s in steps] + [v.get('text', '') for s in steps for v in s['draw']]
    answers = task.get('problem_answers') or task.get('answers') or []
    joined = ''.join(texts)
    if guard.stated_answers(joined, answers) or any(guard.normalize(t) == guard.normalize(a) for t in texts for a in answers):
        raise BoardError('board policy withholds the final answer')
    if any(v['kind'] not in ('text', 'remove') for s in steps for v in s['draw']):
        raise BoardError('board policy permits text hints only while the answer is withheld')


def task_for(ws, doc):
    state = Workspace(str(ws.root.parent), doc['sessionId']).state()
    return next((t for t in state['tasks'] if t['id'] == doc['taskId']), None)


def view(doc):
    return {k: doc[k] for k in ('boardId', 'sessionId', 'taskId', 'title', 'revision', 'generation', 'applied', 'presentation')} | {
        'steps': doc['steps'][:doc['applied']], 'pending': len(doc['steps']) - doc['applied'],
        'playing': doc['run'] is not None}


def run(args):
    ws = Workspace(args.get('cwd'), args.get('session'))
    action = args.get('action')
    if action == 'invalidate':
        invalidate(ws)
        return {'status': 'paused'}
    if action == 'list':
        boards = []
        for path in sorted((ws.root / 'boards').glob('*.json'))[-100:]:
            doc = json.loads(path.read_text(encoding='utf-8'))
            boards.append({k: doc[k] for k in ('boardId', 'title', 'sessionId', 'taskId')})
        return {'boards': boards}
    state = ws.state()
    task = current_task(state)
    board_id = args.get('boardId')
    if not board_id and action not in ('append', 'open'):
        board_id = state.get('board_selection')
    if board_id and (not isinstance(board_id, str) or not ID.fullmatch(board_id)):
        raise BoardError('invalid board id')
    docs = [json.loads(p.read_text(encoding='utf-8')) for p in (ws.root / 'boards').glob('*.json')]
    doc = next((d for d in docs if d['boardId'] == board_id), None) if board_id else next(
        (d for d in docs if d['sessionId'] == ws.session and task and d['taskId'] == task['id']), None)
    if doc is None:
        if action not in ('append', 'open'):
            if action == 'read':
                return None
            raise BoardError('board not found')
        if board_id:
            raise BoardError('board not found')
        policy(task, [])
        doc = {'schema': 'k12-board/1', 'boardId': uuid.uuid4().hex, 'sessionId': ws.session,
               'taskId': task['id'], 'title': task.get('topic') or '板书', 'steps': [], 'applied': 0,
               'revision': 0, 'generation': 0, 'receipts': {}, 'run': None, 'boot': args.get('boot'),
               'presentation': {'intent': 'show', 'sequence': 0, 'autoplay': False}}
    if doc.get('boot') != args.get('boot') or (doc['run'] and doc['run']['until'] < time.time()):
        stop(doc)
        doc['boot'] = args.get('boot')
        save(ws, doc)
    task = task_for(ws, doc)
    if action in ('read', 'claim', 'next', 'open', 'append'):
        policy(task, doc['steps'][:doc['applied']])
    if action == 'read':
        return view(doc)
    if action == 'append':
        if doc['sessionId'] != ws.session or doc['taskId'] != current_task(ws.state())['id']:
            raise BoardError('append requires current task')
        receipt = args.get('receipt')
        if not isinstance(receipt, str) or not receipt or len(receipt) > 200:
            raise BoardError('trusted receipt required')
        steps = args.get('steps')
        validate(steps)
        digest = hashlib.sha256(json.dumps(steps, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        if receipt in doc['receipts']:
            saved = doc['receipts'][receipt]
            if saved['digest'] != digest:
                raise BoardError('receipt reused with different steps')
            return saved['result']
        proposed = doc['steps'] + steps
        if len(proposed) > 64 or sum(len(s['draw']) for s in proposed) > 500:
            raise BoardError('board size limit reached')
        policy(task, proposed)
        doc['steps'] = proposed
        doc['revision'] += 1
        doc['presentation'] = {'intent': 'show', 'sequence': doc['presentation']['sequence'] + 1, 'autoplay': True}
        result = {'boardId': doc['boardId'], 'revision': doc['revision'], 'appended': len(steps), 'status': 'queued'}
        doc['receipts'][receipt] = {'digest': digest, 'result': result}
        save(ws, doc)
        state['board_selection'] = doc['boardId']
        ws.save(state)
        return result
    if action == 'claim':
        if args.get('generation', doc['generation']) != doc['generation']:
            raise BoardError('stale playback')
        if doc['run']:
            raise BoardError('board already playing')
        stop(doc)
        doc['run'] = {'token': uuid.uuid4().hex, 'until': time.time() + 15, 'sessionId': ws.session}
        save(ws, doc)
        return {'token': doc['run']['token'], 'generation': doc['generation']}
    if action in ('next', 'heartbeat', 'release'):
        if not doc['run'] or doc['run']['token'] != args.get('token') or doc['generation'] != args.get('generation'):
            raise BoardError('stale playback')
        if action == 'next':
            if args.get('expectedApplied') != doc['applied']:
                raise BoardError('stale cursor')
            policy(task, doc['steps'][:doc['applied'] + 1])
            doc['applied'] = min(doc['applied'] + 1, len(doc['steps']))
            doc['revision'] += 1
        if action == 'release':
            doc['run'] = None
            doc['revision'] += 1
        else:
            doc['run']['until'] = time.time() + 15
    elif action in ('pause', 'close', 'open'):
        if action == 'pause' and args.get('token') and (not doc['run'] or doc['run']['token'] != args['token'] or doc['generation'] != args.get('generation')):
            raise BoardError('stale playback')
        stop(doc)
        if action != 'pause':
            doc['presentation'] = {'intent': 'show' if action == 'open' else 'close', 'sequence': doc['presentation']['sequence'] + 1, 'autoplay': False}
        if action == 'open':
            state['board_selection'] = doc['boardId']
            ws.save(state)
    else:
        raise BoardError('unknown board action')
    save(ws, doc)
    return view(doc)
