"""Native Study practice: local associations, remote answer and record ownership."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
import datetime as dt
import re
import uuid
import secrets
from urllib.parse import urlsplit

from .session import Workspace
from .study import StudyError, digest, judgment, now, packed
from .study_templates import build
from .study_transport import Transport, TransportError, load_binding, register_notification
from . import study_transport


def _context(args):
    ws = Workspace(args.get('cwd'), args.get('session'))
    binding = load_binding(str(ws.root.parent))
    if not binding:
        raise TransportError('blocked', 'workspace unbound')
    return ws, binding


@contextmanager
def _database(ws):
    ws.root.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ws.root / 'study-practice.sqlite', timeout=90) as db:
        db.row_factory = sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(key TEXT PRIMARY KEY,session TEXT NOT NULL,
          binding TEXT NOT NULL,payload TEXT NOT NULL,task_id TEXT UNIQUE,view TEXT,
          state TEXT NOT NULL,reason TEXT,read_at TEXT,submission_id TEXT,
          continued INTEGER NOT NULL DEFAULT 0,grade_wire TEXT,grade_receipt TEXT);
        ''')
        columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
        for name, spec in (('watch_armed', 'INTEGER NOT NULL DEFAULT 0'),
                           ('watch_state', "TEXT NOT NULL DEFAULT 'pending'"),
                           ('watch_reason', 'TEXT'), ('watch_next', 'REAL NOT NULL DEFAULT 0'),
                           ('watch_lease', 'REAL NOT NULL DEFAULT 0'), ('watch_owner', 'TEXT')):
            if name not in columns:
                db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {spec}')
        db.execute('BEGIN IMMEDIATE')
        workspace = os.path.normcase(str(ws.root.parent.resolve()))
        previous = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        if previous and previous['value'] != workspace:
            raise TransportError('blocked', 'copied practice associations require a new workspace')
        db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace',?)", (workspace,))
        yield db


def _target(binding):
    return {k: binding[k] for k in ('studentId', 'endpoint', 'bindingVersion')}


def _issue_payload(root, session, binding, assessment, slot, purpose):
    seed = packed([str(Path(root).resolve()), session, binding['studentId'], binding['endpoint'],
                   assessment, slot, purpose])
    key = digest(seed)
    return key, {'exerciseId': 'web-' + key[:40], 'taskRef': 'web-' + key[24:64],
                 'assessment': assessment, 'slot': slot, 'purpose': purpose}


def _association(db, args, binding):
    if args.get('taskId'):
        row = db.execute('SELECT * FROM tasks WHERE task_id=? AND session=?',
                         (args['taskId'], args['session'])).fetchone()
    else:
        row = db.execute('SELECT * FROM tasks WHERE session=? ORDER BY rowid DESC LIMIT 1',
                         (args['session'],)).fetchone()
    if not row:
        raise TransportError('blocked', 'no practice task for this session')
    old = json.loads(row['binding'])
    if (any(old[k] != binding[k] for k in ('studentId', 'endpoint'))
            or old['bindingVersion'] > binding['bindingVersion']):
        raise TransportError('blocked', 'practice binding mismatch')
    payload = json.loads(row['payload'])
    if 'hostWindow' in payload:
        trusted = _web_baseline_row(binding['workspace'], args['session'], row['task_id'])
        if (not trusted or json.loads(trusted['payload']) != payload
                or any(json.loads(trusted['binding'])[k] != binding[k] for k in ('studentId', 'endpoint'))):
            raise TransportError('blocked', 'independent assessment association mismatch')
        return row
    _, expected = _issue_payload(binding['workspace'], args['session'], binding,
                                 payload['assessment'], payload['slot'], payload['purpose'])
    if payload != expected:
        raise TransportError('blocked', 'practice original session association mismatch')
    return row


def _view(value, binding, task_id=None):
    if (not isinstance(value, dict) or value.get('studentId') != binding['studentId']
            or not isinstance(value.get('taskId'), str)
            or (task_id and value['taskId'] != task_id)
            or value.get('status') not in ('issued', 'submitted')
            or not isinstance(value.get('exercise'), dict)):
        raise TransportError('blocked', 'invalid remote task identity')
    exercise = value['exercise']
    if not exercise.get('snapshotHash') or not isinstance(exercise.get('question'), dict):
        raise TransportError('blocked', 'invalid remote frozen question')
    link = value.get('answerLink')
    if not isinstance(link, str) or urlsplit(link)[:2] != urlsplit(binding['endpoint'])[:2]:
        raise TransportError('blocked', 'invalid remote task origin')
    submission = value.get('submission')
    if value['status'] == 'submitted':
        if (not isinstance(submission, dict) or submission.get('observation') not in ('practice_only', 'pending', 'eligible')
                or not all(isinstance(submission.get(k), str) and submission[k]
                           for k in ('submissionId', 'recordId', 'submittedAt'))):
            raise TransportError('blocked', 'invalid remote submission receipt')
        text = submission.get('answerText')
        if submission.get('deletedAt'):
            if text is not None:
                raise TransportError('blocked', 'deleted remote answer was returned')
        elif not isinstance(text, str) or digest(text) != submission['textHash']:
            raise TransportError('blocked', 'remote answer hash mismatch')
    elif submission is not None:
        raise TransportError('blocked', 'invalid issued task submission')
    return value


def _safe_view(view):
    # The answer remains solely in Study. Local storage is a recovery association.
    result = json.loads(packed(view))
    if result.get('submission'):
        result['submission'].pop('answerText', None)
    return result


def _save(db, key, view, read=False):
    submission = view.get('submission')
    db.execute('UPDATE tasks SET task_id=?,view=?,state=?,reason=NULL WHERE key=?',
               (view['taskId'], packed(_safe_view(view)), view['status'], key))
    if read and submission:
        db.execute('UPDATE tasks SET read_at=?,submission_id=? WHERE key=?',
                   (now(), submission['submissionId'], key))


def _read(client, row, binding):
    view = _view(client.call('task_show', record_id=row['task_id']), binding, row['task_id'])
    payload = json.loads(row['payload'])
    if view.get('hostWindow') != payload.get('hostWindow'):
        raise TransportError('blocked', 'remote assessment baseline mismatch')
    if any(view['exercise'].get(k) != payload[k]
           for k in ('exerciseId', 'taskRef', 'slot', 'purpose', 'assessment')):
        raise TransportError('blocked', 'remote exercise association mismatch')
    if row['view'] and json.loads(row['view'])['exercise'] != view['exercise']:
        raise TransportError('blocked', 'remote frozen question changed')
    submission = view.get('submission')
    if submission:
        detail = client.call('detail', record_id=submission['recordId'])
        tutor = detail.get('tutor', {}) if isinstance(detail, dict) else {}
        if (tutor.get('recordId') != submission['recordId']
                or type(tutor.get('revision')) is not int or tutor['revision'] < 1):
            raise TransportError('blocked', 'invalid canonical record receipt')
        if bool(tutor.get('deletedAt')) != bool(submission.get('deletedAt')):
            raise TransportError('blocked', 'remote deletion changed; read submission again')
        if not tutor.get('deletedAt'):
            evidence = (tutor.get('record') or {}).get('submission', {}).get('evidence', {})
            if (evidence.get('source') != 'web_task'
                    or evidence.get('taskId') != view['taskId']
                    or evidence.get('submissionId') != submission['submissionId']
                    or evidence.get('textHash') != submission['textHash']
                    or evidence.get('selectedText') != submission['answerText']):
                raise TransportError('blocked', 'canonical web submission mismatch')
        view = {**view, 'recordRevision': tutor['revision']}
    if 'hostWindow' in payload:
        baseline = _web_baseline_row(binding['workspace'], row['session'], row['task_id'])
        if baseline and baseline.get('terminal_denial'):
            view = {**view, 'hostCertificationDenied': True}
    return view


def run(args):
    action = args.get('action')
    allowed = {'issue': {'assessment', 'slot', 'purpose'}, 'read': {'taskId'},
               'grade': {'taskId', 'judgment'}}
    if action not in allowed or set(args) - {'cwd', 'session', 'action'} - allowed[action]:
        raise StudyError('invalid practice command fields')
    try:
        ws, binding = _context(args)
    except TransportError as exc:
        return {'state': exc.state, 'reason': exc.reason}
    with _database(ws) as db:
        row = None
        try:
            if action == 'issue':
                build(args.get('assessment'))
                if (args.get('slot') not in ('diagnose-1', 'practice-1', 'transfer-1', 'review-1', 'reflection-1')
                        or args.get('purpose') not in ('diagnosis', 'practice', 'transfer', 'review')):
                    raise StudyError('invalid assessment slot or purpose')
                key, payload = _issue_payload(ws.root.parent, args['session'], binding,
                                              args['assessment'], args['slot'], args['purpose'])
                db.execute("INSERT OR IGNORE INTO tasks(key,session,binding,payload,state,watch_armed) VALUES (?,?,?,?,'pending',1)",
                           (key, args['session'], packed(_target(binding)), packed(payload)))
                row = db.execute('SELECT * FROM tasks WHERE key=?', (key,)).fetchone()
                # Commit the stable identity before an uncertain network operation.
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                client = Transport(binding)
                view = (_read(client, row, binding) if row['task_id'] else
                        _view(client.call('task_issue', payload=json.loads(row['payload'])), binding))
                if any(view['exercise'].get(k) != payload[k]
                       for k in ('exerciseId', 'taskRef', 'slot', 'purpose', 'assessment')):
                    raise TransportError('blocked', 'remote exercise association mismatch')
            else:
                if not args.get('taskId'):
                    raise TransportError('blocked', 'exact practice task required')
                row = _association(db, args, binding)
                if not row['task_id']:
                    raise TransportError('pending', 'task issue has not been confirmed; retry issue')
                client = Transport(binding)
                view = _read(client, row, binding)
                if view.get('submission') and 'hostWindow' in json.loads(row['payload']):
                    baseline = _web_baseline_row(args['cwd'], args['session'], row['task_id'])
                    if not baseline or (not baseline['receipt'] and not baseline.get('terminal_denial')):
                        raise TransportError('pending', 'independent assessment evidence has not been sealed')
                if action == 'grade':
                    desired = judgment(args.get('judgment'))
                    submission = view.get('submission')
                    if not submission or submission.get('deletedAt'):
                        raise TransportError('blocked', 'no live web submission to grade')
                    prior = json.loads(row['grade_wire']) if row['grade_wire'] else None
                    prior_receipt = json.loads(row['grade_receipt']) if row['grade_receipt'] else None
                    if (prior and prior['judgment'] == desired
                            and (not prior_receipt or prior_receipt['appliedRevision'] == view['recordRevision'])):
                        wire = prior
                    else:
                        if prior and not row['grade_receipt']:
                            raise TransportError('conflict', 'resolve the pending grade before changing judgment')
                        wire = {'mutationId': 'grade-' + digest(packed([row['key'], view['recordRevision'], desired]))[:40],
                                'expectedRevision': view['recordRevision'], 'judgment': desired}
                        db.execute('UPDATE tasks SET grade_wire=?,grade_receipt=NULL WHERE key=?',
                                   (packed(wire), row['key']))
                        db.commit()
                        db.execute('BEGIN IMMEDIATE')
                    receipt = client.call('update', payload=wire, record_id=submission['recordId'])
                    if (receipt.get('recordId') != submission['recordId']
                            or receipt.get('mutationId') != wire['mutationId']
                            or receipt.get('appliedRevision') != wire['expectedRevision'] + 1):
                        raise TransportError('blocked', 'invalid remote grade receipt')
                    db.execute('UPDATE tasks SET grade_receipt=? WHERE key=?', (packed(receipt), row['key']))
                    view = _read(client, row, binding)
                    view['gradeReceipt'] = receipt
            _save(db, row['key'], view, read=action in ('read', 'grade'))
            saved = db.execute('SELECT * FROM tasks WHERE key=?', (row['key'],)).fetchone()
            return {'state': 'complete', 'source': 'web_submission',
                    **_latest(saved), **view}
        except TransportError as exc:
            if row:
                db.execute('UPDATE tasks SET state=?,reason=? WHERE key=?', (exc.state, exc.reason, row['key']))
            return {'state': exc.state, 'reason': exc.reason}


def _latest(row):
    if not row or not row['view']:
        return None
    view = json.loads(row['view'])
    watch_state = row['watch_state'] if 'watch_state' in row.keys() else None
    watch_reason = row['watch_reason'] if 'watch_reason' in row.keys() else None
    independent = 'hostWindow' in json.loads(row['payload'])
    submission = view.get('submission') or {}
    denied = view.get('hostCertificationDenied', False)
    return {'taskId': view['taskId'], 'questionText': view['exercise']['question']['stem'],
            'answerUrl': view['answerLink'], 'status': row['state'],
            'submission': view.get('submission'), 'recordRevision': view.get('recordRevision'),
            'readbackAt': row['read_at'], 'continued': bool(row['continued']),
            'watchState': watch_state, 'watchReason': watch_reason,
            'canRetryContinuation': not row['continued'] and watch_state in ('paused', 'blocked', 'conflict'),
            'recoveryReason': watch_reason,
            'assessmentMode': 'host_ui_assessment/v1' if independent else None,
            'observation': 'practice_only' if denied else submission.get('observation', 'pending' if independent else 'practice_only'),
            'observationReason': 'issuer_not_authorized' if denied else submission.get('observationReason'),
            'observationEnabled': False if denied else submission.get('observationEnabled', False),
            'observationMode': submission.get('observationMode')}


def status(args):
    ws = Workspace(args.get('cwd'), args.get('session'))
    path = ws.root / 'study-practice.sqlite'
    result = {'counts': {'pending': 0, 'blocked': 0, 'conflict': 0}, 'lastReceiptAt': None, 'latestTask': None}
    sync_path = ws.root / 'study-sync.sqlite'
    if sync_path.is_file():
        with sqlite3.connect(sync_path.as_uri() + '?mode=ro', uri=True) as db:
            for state, count in db.execute('''SELECT o.state,COUNT(*) FROM outbox o
                    JOIN records r ON r.event_id=o.event_id
                    JOIN exercises e ON e.id=r.exercise_id WHERE e.session=? GROUP BY o.state''',
                                          (ws.session,)):
                if state in result['counts']:
                    result['counts'][state] += count
    if not path.is_file():
        return result
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute('SELECT state,COUNT(*) AS n FROM tasks WHERE session=? GROUP BY state', (ws.session,)):
            if row['state'] in result['counts']:
                result['counts'][row['state']] += row['n']
        latest = db.execute('SELECT * FROM tasks WHERE session=? ORDER BY rowid DESC LIMIT 1', (ws.session,)).fetchone()
        result['latestTask'] = _latest(latest)
        result['lastReceiptAt'] = latest['read_at'] if latest else None
    return result


def continuation(args):
    if not args.get('taskId'):
        raise TransportError('blocked', 'exact practice task required')
    ws, binding = _context(args)
    with _database(ws) as db:
        row = _association(db, args, binding)
        view = json.loads(row['view']) if row['view'] else {}
        submission = view.get('submission') or {}
        if not row['read_at'] or not row['submission_id'] or submission.get('deletedAt'):
            raise TransportError('blocked', 'read a live web submission before continuing')
        baseline = _web_baseline_row(args['cwd'], args['session'], row['task_id'])
        if baseline and not baseline['receipt'] and not baseline.get('terminal_denial'):
            raise TransportError('pending', 'independent assessment evidence has not been sealed')
        current = _read(Transport(binding), row, binding)
        if not current.get('submission') or current['submission'].get('deletedAt'):
            raise TransportError('blocked', 'no live web submission to continue')
        _save(db, row['key'], current, read=True)
        submission_id = current['submission']['submissionId']
        exercise = current['exercise']
        issue_key, _ = _issue_payload(ws.root.parent, args['session'], binding,
                                      exercise['assessment'], exercise['slot'], exercise['purpose'])
        if baseline:
            issue_key = baseline['issue_key']
        receipt = {'requestId': 'web-continue-' + digest(issue_key + submission_id)[:40],
                'taskId': current['taskId'], 'submissionId': submission_id,
                'continued': bool(row['continued'])}
        register_notification(args['cwd'], args['session'], receipt)
        return receipt


def mark_continued(args):
    expected = continuation(args)
    if any(args.get(k) != expected[k] for k in ('requestId', 'taskId', 'submissionId')):
        raise TransportError('blocked', 'continuation association mismatch')
    ws, binding = _context(args)
    with _database(ws) as db:
        row = _association(db, args, binding)
        _lease_owner(row, args)
        db.execute("UPDATE tasks SET continued=1,watch_armed=0,watch_state='delivered',watch_lease=0,watch_owner=NULL WHERE key=?", (row['key'],))
    return {**expected, 'continued': True}


def open_task(args):
    """Host-only navigation. The transient capability is never saved in a view."""
    if set(args) != {'cwd', 'session', 'taskId'} or not args.get('taskId'):
        raise TransportError('blocked', 'exact practice task required')
    ws, binding = _context(args)
    with _database(ws) as db:
        row = _association(db, args, binding)
        client = Transport(binding)
        view = _read(client, row, binding)
        if view.get('submission', {}) and view['submission'].get('deletedAt'):
            raise TransportError('blocked', 'practice submission was deleted')
        result = client.call('task_open', record_id=row['task_id'])
        if (not isinstance(result, dict) or set(result) != {'handoffUrl', 'expiresAt'}
                or not all(isinstance(v, str) for v in result.values())):
            raise TransportError('blocked', 'invalid task handoff response')
        try:
            parsed = urlsplit(result['handoffUrl'])
            expiry = dt.datetime.fromisoformat(result['expiresAt'].replace('Z', '+00:00'))
            valid = (parsed[:2] == urlsplit(binding['endpoint'])[:2]
                     and parsed.path == '/practice/' + row['task_id'] and not parsed.query
                     and re.fullmatch(r'handoff=[A-Za-z0-9_-]{43}', parsed.fragment)
                     and 0 < (expiry - dt.datetime.now(dt.timezone.utc)).total_seconds() <= 180)
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise TransportError('blocked', 'invalid task handoff response')
        _save(db, row['key'], view)
        db.execute("UPDATE tasks SET watch_armed=1,watch_state='pending',watch_reason=NULL,watch_next=0 WHERE key=? AND continued=0",
                   (row['key'],))
        return {'handoffUrl': result['handoffUrl'], 'expiresAt': result['expiresAt']}


def watch_state(args):
    if args.get('state') not in ('pending', 'paused', 'blocked', 'conflict', 'delivered'):
        raise TransportError('blocked', 'invalid watch state')
    delay = args.get('retryAfterSeconds', 3 if args['state'] == 'pending' else 30)
    if type(delay) not in (int, float) or not 0 <= delay <= 300:
        raise TransportError('blocked', 'invalid watch retry delay')
    reason = args.get('reason')
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise TransportError('blocked', 'invalid watch reason')
    if not args.get('taskId'):
        raise TransportError('blocked', 'exact practice task required')
    ws, binding = _context(args)
    with _database(ws) as db:
        row = _association(db, args, binding)
        _lease_owner(row, args)
        db.execute('''UPDATE tasks SET watch_state=?,watch_reason=?,watch_next=?,watch_lease=0,watch_owner=NULL,
                      watch_armed=CASE WHEN ?='delivered' THEN 0 ELSE watch_armed END WHERE key=?''',
                   (args['state'], reason, time.time() + delay, args['state'], row['key']))
    return {'taskId': row['task_id'], 'state': args['state']}


def _lease_owner(row, args):
    if row['watch_owner'] and row['watch_owner'] != args.get('leaseId'):
        raise TransportError('pending', 'practice watch is owned by another admission')
    if args.get('leaseId') and row['watch_owner'] != args['leaseId']:
        raise TransportError('pending', 'practice watch lease changed')


def watch_claim(args):
    if not args.get('taskId'):
        raise TransportError('blocked', 'exact practice task required')
    ws, binding = _context(args)
    with _database(ws) as db:
        row = _association(db, args, binding)
        if row['continued'] or row['watch_lease'] > time.time():
            return {'claimed': False}
        owner = uuid.uuid4().hex
        db.execute('UPDATE tasks SET watch_lease=?,watch_owner=? WHERE key=?',
                   (time.time() + 600, owner, row['key']))
        return {'claimed': True, 'leaseId': owner}


def armed_tasks(binding, limit=30):
    """Discover due work only from an administrator-known root; admission claims it."""
    root = Path(binding['workspace'])
    path = root / '.k12' / 'study-practice.sqlite'
    if not root.is_dir() or not path.is_file():
        return []
    # A read-only preflight prevents migration/arming historical ledgers during enumeration.
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        if 'watch_armed' not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:
            return []
    ws = Workspace(str(root), 'watch-enumeration')
    results = []
    with _database(ws) as db:
        rows = db.execute('''SELECT * FROM tasks WHERE watch_armed=1 AND continued=0
                AND task_id IS NOT NULL AND watch_next<=? AND watch_lease<=? ORDER BY watch_next,rowid LIMIT ?''',
                          (time.time(), time.time(), limit)).fetchall()
        for row in rows:
            try:
                _association(db, {'taskId': row['task_id'], 'session': row['session']}, binding)
            except TransportError:
                continue
            results.append({'cwd': str(root), 'session': row['session'], 'taskId': row['task_id'],
                            **_target(binding), 'watchState': row['watch_state'], 'watchReason': row['watch_reason']})
    return results


@contextmanager
def _web_admin(cwd):
    path = study_transport._store()
    if not path.is_file() or path.resolve().is_relative_to(Path(cwd).resolve()):
        raise TransportError('blocked', 'trusted assessment store unavailable')
    with sqlite3.connect(path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS web_assessments(
          issue_key TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,workspace TEXT NOT NULL,
          session TEXT NOT NULL,binding TEXT NOT NULL,payload TEXT NOT NULL,task_id TEXT UNIQUE,
          snapshot_hash TEXT,exposed INTEGER NOT NULL DEFAULT 0,proof TEXT,receipt TEXT,
          revoke TEXT,revoke_receipt TEXT)''')
        columns = {r[1] for r in db.execute('PRAGMA table_info(web_assessments)')}
        for name, spec in (('nonce', 'TEXT'), ('receipt_snapshot', 'TEXT'), ('audit_checked', 'REAL NOT NULL DEFAULT 0'),
                           ('terminal_denial', 'TEXT')):
            if name not in columns:
                db.execute(f'ALTER TABLE web_assessments ADD COLUMN {name} {spec}')
        db.execute('''CREATE TABLE IF NOT EXISTS web_session_exposures(
                      workspace TEXT NOT NULL,session TEXT NOT NULL,PRIMARY KEY(workspace,session))''')
        db.execute('BEGIN IMMEDIATE')
        yield db


def _web_baseline_row(cwd, session, task_id):
    path = study_transport._store()
    if not path.is_file():
        return None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='web_assessments'").fetchone():
            return None
        row = db.execute('SELECT * FROM web_assessments WHERE workspace=? AND session=? AND task_id=?',
                         (study_transport._workspace(cwd), session, task_id)).fetchone()
        return dict(row) if row else None


def _window(value, session):
    if (not isinstance(value, dict) or set(value) != {'version', 'sessionId', 'fromEventSeq', 'prefixHash', 'clean'}
            or value['version'] != 'web-host/v1' or value['sessionId'] != session
            or type(value['fromEventSeq']) is not int or value['fromEventSeq'] < -1
            or type(value['clean']) is not bool or not _hash_string(value['prefixHash'])):
        raise TransportError('blocked', 'invalid trusted assessment baseline')
    return value


def _hash_string(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value)


def web_assessment(args):
    if set(args) != {'cwd', 'session', 'templateId', 'hostWindow'}:
        raise TransportError('blocked', 'invalid private assessment fields')
    ws, binding = _context(args)
    window = _window(args['hostWindow'], ws.session)
    from .study_templates import MANIFEST
    if args['templateId'] not in MANIFEST:
        raise TransportError('blocked', 'unknown assessment template')
    root = study_transport._workspace(args['cwd'])
    window = dict(window)
    request_key = digest(packed([root, ws.session, binding['studentId'], binding['endpoint'], args['templateId'], window]))
    with _web_admin(root) as admin:
        saved = admin.execute('SELECT * FROM web_assessments WHERE request_key=?', (request_key,)).fetchone()
        if not saved:
            if admin.execute('SELECT 1 FROM web_session_exposures WHERE workspace=? AND session=?', (root, ws.session)).fetchone():
                window['clean'] = False
            coefficient = secrets.choice([n for n in range(-20, 21) if n])
            params = ({'k': coefficient, 'b': secrets.randbelow(41) - 20, 'x': secrets.randbelow(41) - 20}
                      if args['templateId'] == 'linear-function-value/v1' else
                      {'a': coefficient, 'b': secrets.randbelow(41) - 20, 'c': secrets.randbelow(41) - 20})
            if args['templateId'] == 'linear-inequality/v1':
                params['op'] = secrets.choice(['<', '>'])
            assessment = {'templateId': args['templateId'], 'params': params}
            build(assessment)
            nonce = secrets.token_hex(24)
            issue_key = digest(packed([request_key, nonce, assessment, window]))
            payload = {'exerciseId': 'web-' + issue_key[:40], 'taskRef': 'web-' + issue_key[24:64],
                       'slot': 'practice-1', 'purpose': 'diagnosis', 'assessment': assessment, 'hostWindow': window}
            admin.execute('''INSERT INTO web_assessments(issue_key,request_key,workspace,session,binding,payload,nonce)
                             VALUES (?,?,?,?,?,?,?)''',
                          (issue_key, request_key, root, ws.session, packed(_target(binding)), packed(payload), nonce))
            saved = admin.execute('SELECT * FROM web_assessments WHERE request_key=?', (request_key,)).fetchone()
        baseline = dict(saved)
    payload = json.loads(baseline['payload'])
    window = payload['hostWindow']
    target = json.loads(baseline['binding'])
    if any(target[k] != binding[k] for k in ('studentId', 'endpoint')):
        raise TransportError('blocked', 'assessment binding mismatch')
    # The private identity is durable outside the workspace before contacting Study.
    client = Transport(binding)
    with _database(ws) as db:
        db.execute("INSERT OR IGNORE INTO tasks(key,session,binding,payload,state,watch_armed) VALUES (?,?,?,?,'pending',1)",
                   (baseline['issue_key'], ws.session, baseline['binding'], baseline['payload']))
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM tasks WHERE key=?', (baseline['issue_key'],)).fetchone()
        try:
            view = _view(client.call('task_show', record_id=baseline['task_id']) if baseline['task_id'] else
                         client.call('task_assessment', payload=payload), binding, baseline['task_id'])
            if (view.get('hostWindow') != window or any(view['exercise'].get(k) != payload[k]
                    for k in ('exerciseId', 'taskRef', 'slot', 'purpose', 'assessment'))):
                raise TransportError('blocked', 'remote assessment baseline mismatch')
            with _web_admin(root) as admin:
                admin.execute('UPDATE web_assessments SET task_id=?,snapshot_hash=? WHERE issue_key=?',
                              (view['taskId'], view['exercise']['snapshotHash'], baseline['issue_key']))
            _save(db, row['key'], view)
            saved = db.execute('SELECT * FROM tasks WHERE key=?', (row['key'],)).fetchone()
            # Issue never exposes a submitted answer before the common certification gate.
            return {'state': 'complete', 'source': 'web_submission', **_latest(saved)}
        except TransportError as exc:
            db.execute('UPDATE tasks SET state=?,reason=? WHERE key=?', (exc.state, exc.reason, row['key']))
            return {'state': exc.state, 'reason': exc.reason}


def web_baseline(args):
    ws, binding = _context(args)
    row = _web_baseline_row(args['cwd'], ws.session, args.get('taskId'))
    if not row:
        return None
    target = json.loads(row['binding'])
    if any(target[k] != binding[k] for k in ('studentId', 'endpoint')):
        raise TransportError('blocked', 'assessment binding mismatch')
    return {'taskId': row['task_id'], 'hostWindow': json.loads(row['payload'])['hostWindow'],
            'proof': json.loads(row['proof']) if row['proof'] else None,
            'exposed': bool(row['exposed']),
            'terminalDenied': bool(row.get('terminal_denial')),
            'reason': 'issuer_not_authorized' if row.get('terminal_denial') else None,
            'certificationReceipt': json.loads(row['receipt']) if row['receipt'] else None,
            'revocation': json.loads(row['revoke']) if row['revoke'] else None}


def web_read(args):
    ws, binding = _context(args)
    if not args.get('taskId'):
        raise TransportError('blocked', 'exact practice task required')
    with _database(ws) as db:
        row = _association(db, args, binding)
        return _read(Transport(binding), row, binding)


def web_certify(args):
    if set(args) != {'cwd', 'session', 'taskId', 'proof'}:
        raise TransportError('blocked', 'invalid private certification fields')
    ws, binding = _context(args)
    baseline = _web_baseline_row(args['cwd'], ws.session, args['taskId'])
    if not baseline:
        raise TransportError('blocked', 'trusted assessment baseline missing')
    if baseline.get('terminal_denial'):
        return {'state': 'excluded', 'terminalDenied': True, 'reason': 'issuer_not_authorized'}
    view = web_read({k: args[k] for k in ('cwd', 'session', 'taskId')})
    submission = view.get('submission')
    if not submission or submission.get('deletedAt'):
        raise TransportError('blocked', 'no live submission to certify')
    proof = args['proof']
    fields = {'submissionId', 'textHash', 'snapshotHash', 'sessionId', 'fromEventSeq', 'throughEventSeq',
              'prefixHash', 'sealHash', 'complete', 'clean', 'reason'}
    window = json.loads(baseline['payload'])['hostWindow']
    if (not isinstance(proof, dict) or set(proof) != fields
            or any(proof[k] != window[k] for k in ('sessionId', 'fromEventSeq', 'prefixHash'))
            or proof['submissionId'] != submission['submissionId'] or proof['textHash'] != submission['textHash']
            or proof['snapshotHash'] != baseline['snapshot_hash'] or proof['snapshotHash'] != view['exercise']['snapshotHash']
            or type(proof['throughEventSeq']) is not int or proof['throughEventSeq'] < proof['fromEventSeq']
            or type(proof['complete']) is not bool or type(proof['clean']) is not bool
            or not _hash_string(proof['sealHash'])
            or proof['reason'] not in ('clean', 'assisted', 'prior_input', 'evidence_incomplete')
            or (proof['clean'] and (not proof['complete'] or proof['reason'] != 'clean' or not window['clean']))
            or (not proof['clean'] and proof['reason'] == 'clean')):
        raise TransportError('blocked', 'certification does not match the trusted baseline and submission')
    with _web_admin(args['cwd']) as admin:
        current = admin.execute('SELECT * FROM web_assessments WHERE issue_key=?', (baseline['issue_key'],)).fetchone()
        if proof['clean'] and current['exposed']:
            raise TransportError('blocked', 'assessment was exposed to prior teaching content')
        if current['proof'] and current['proof'] != packed(proof):
            raise TransportError('conflict', 'first assessment seal is immutable')
        snapshot = {'taskId': view['taskId'], 'exerciseId': view['exercise']['exerciseId'],
                    'snapshotHash': view['exercise']['snapshotHash'], 'assessment': view['exercise']['assessment'],
                    'submissionId': submission['submissionId'], 'recordId': submission['recordId'],
                    'textHash': submission['textHash'], 'submittedAt': submission['submittedAt']}
        if current['receipt_snapshot'] and current['receipt_snapshot'] != packed(snapshot):
            raise TransportError('conflict', 'canonical first submission changed')
        admin.execute('UPDATE web_assessments SET proof=?,receipt_snapshot=? WHERE issue_key=?',
                      (packed(proof), packed(snapshot), baseline['issue_key']))
    try:
        receipt = Transport(binding).call('task_certify', record_id=args['taskId'], payload=proof)
    except TransportError as exc:
        if exc.code not in ('UNAUTHORIZED', 'FORBIDDEN'):
            raise
        with _web_admin(args['cwd']) as admin:
            admin.execute('UPDATE web_assessments SET terminal_denial=? WHERE issue_key=?',
                          (exc.code, baseline['issue_key']))
        return {'state': 'excluded', 'terminalDenied': True, 'reason': 'issuer_not_authorized'}
    _cert_receipt(receipt, args['taskId'], submission)
    with _web_admin(args['cwd']) as admin:
        admin.execute('UPDATE web_assessments SET receipt=? WHERE issue_key=?', (packed(receipt), baseline['issue_key']))
    return {'state': 'complete', 'receipt': receipt, 'task': web_read({k: args[k] for k in ('cwd', 'session', 'taskId')})}


def _cert_receipt(receipt, task_id, submission):
    if (not isinstance(receipt, dict) or receipt.get('taskId') != task_id
            or receipt.get('submissionId') != submission['submissionId']
            or receipt.get('recordId') != submission['recordId'] or not isinstance(receipt.get('certifiedAt'), str)):
        raise TransportError('blocked', 'invalid remote certification receipt')


def web_revoke(args):
    if set(args) != {'cwd', 'session', 'taskId', 'submissionId', 'reason', 'sealHash'}:
        raise TransportError('blocked', 'invalid private revocation fields')
    ws, binding = _context(args)
    baseline = _web_baseline_row(args['cwd'], ws.session, args['taskId'])
    if (not baseline or not baseline['proof'] or args['reason'] not in ('assisted', 'prior_input', 'evidence_incomplete')
            or not _hash_string(args['sealHash']) or json.loads(baseline['proof'])['submissionId'] != args['submissionId']):
        raise TransportError('blocked', 'invalid assessment revocation')
    body = {k: args[k] for k in ('submissionId', 'reason', 'sealHash')}
    with _web_admin(args['cwd']) as admin:
        current = admin.execute('SELECT revoke FROM web_assessments WHERE issue_key=?', (baseline['issue_key'],)).fetchone()
        if current['revoke']:
            body = json.loads(current['revoke'])
        else:
            admin.execute('UPDATE web_assessments SET revoke=? WHERE issue_key=?', (packed(body), baseline['issue_key']))
    receipt = Transport(binding).call('task_revoke_certification', record_id=args['taskId'], payload=body)
    view = web_read({k: args[k] for k in ('cwd', 'session', 'taskId')})
    _cert_receipt(receipt, args['taskId'], view['submission'])
    if not isinstance(receipt.get('revokedAt'), str):
        raise TransportError('blocked', 'invalid remote revocation receipt')
    with _web_admin(args['cwd']) as admin:
        admin.execute('UPDATE web_assessments SET revoke_receipt=? WHERE issue_key=?', (packed(receipt), baseline['issue_key']))
    return {'state': 'complete', 'receipt': receipt}


def web_exposure(args):
    if set(args) != {'cwd', 'session', 'kind'} or args['kind'] not in ('whiteboard', 'history'):
        raise TransportError('blocked', 'invalid assessment exposure')
    ws, _ = _context(args)
    with _web_admin(args['cwd']) as admin:
        admin.execute('INSERT OR IGNORE INTO web_session_exposures VALUES (?,?)',
                      (study_transport._workspace(args['cwd']), ws.session))
        cursor = admin.execute('UPDATE web_assessments SET exposed=1 WHERE workspace=? AND proof IS NULL',
                               (study_transport._workspace(str(ws.root.parent)),))
        return {'marked': cursor.rowcount}


def web_audit_list(args):
    if args:
        raise TransportError('blocked', 'assessment audit accepts no overrides')
    bindings = study_transport.known_bindings()
    if not bindings:
        return {'items': []}
    roots = [study_transport._workspace(binding['workspace']) for binding in bindings if Path(binding['workspace']).is_dir()]
    if not roots:
        return {'items': []}
    items = []
    with _web_admin(roots[0]) as admin:
        placeholders = ','.join('?' for _ in roots)
        rows = admin.execute(f'''SELECT * FROM web_assessments WHERE workspace IN ({placeholders}) AND proof IS NOT NULL
                 AND revoke_receipt IS NULL AND terminal_denial IS NULL ORDER BY audit_checked,rowid LIMIT 30''', roots).fetchall()
        for row in rows:
            admin.execute('UPDATE web_assessments SET audit_checked=? WHERE issue_key=?', (time.time(), row['issue_key']))
            items.append({'cwd': row['workspace'], 'session': row['session'], 'taskId': row['task_id'],
                          'proof': json.loads(row['proof']), 'hostWindow': json.loads(row['payload'])['hostWindow'],
                          'exposed': bool(row['exposed']), 'revocation': json.loads(row['revoke']) if row['revoke'] else None})
    return {'items': items}
