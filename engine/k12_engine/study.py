"""SQLite evidence ledger. Model commands reference captured inputs, never supply them."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .session import Workspace

class StudyError(ValueError):
    pass

def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()

def uid():
    return str(uuid.uuid4())

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')

@contextmanager
def database(args):
    ws = Workspace(args.get('cwd'), args.get('session'))
    ws.root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(ws.root / 'study-sync.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS evidence(capture_order INTEGER PRIMARY KEY AUTOINCREMENT,
      ref TEXT UNIQUE NOT NULL, session TEXT NOT NULL, message_id TEXT NOT NULL,
      role TEXT NOT NULL, text TEXT NOT NULL, hash TEXT NOT NULL, seq INTEGER,
      attempted_at TEXT, captured_at TEXT NOT NULL, coverage TEXT NOT NULL,
      dismissed TEXT, UNIQUE(session,message_id));
    CREATE TABLE IF NOT EXISTS exercises(id TEXT PRIMARY KEY, session TEXT NOT NULL,
      task TEXT NOT NULL, slot TEXT NOT NULL, snapshot TEXT NOT NULL,
      opened_order INTEGER NOT NULL, first_whole TEXT, UNIQUE(session,task,slot));
    CREATE TABLE IF NOT EXISTS records(event_id TEXT PRIMARY KEY, exercise_id TEXT NOT NULL,
      response_ref TEXT NOT NULL, answer_slot TEXT NOT NULL, record TEXT,
      local_revision INTEGER NOT NULL, canonical_revision INTEGER, record_id TEXT,
      sync TEXT NOT NULL, UNIQUE(exercise_id,response_ref,answer_slot));
    CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
      local_revision INTEGER NOT NULL, operation TEXT NOT NULL, intent TEXT NOT NULL,
      binding TEXT, base_revision INTEGER, dependency TEXT, wire TEXT,
      state TEXT NOT NULL, lease_until REAL NOT NULL DEFAULT 0, receipt TEXT,
      reason TEXT, UNIQUE(event_id,local_revision));
    CREATE TABLE IF NOT EXISTS safety_revisions(outbox_id TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS pending_metadata(ref TEXT PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS host_scans(session TEXT PRIMARY KEY, through_seq INTEGER NOT NULL,
      complete INTEGER NOT NULL, hazards TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS exercise_windows(exercise_id TEXT PRIMARY KEY, opened_seq INTEGER,
      fingerprint TEXT, clean INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS seen_assessments(session TEXT NOT NULL, fingerprint TEXT NOT NULL,
      PRIMARY KEY(session,fingerprint));
    CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, value TEXT NOT NULL, observed_at TEXT NOT NULL);
    ''')
    db.execute('BEGIN IMMEDIATE')
    db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('instance', uid()))
    try:
        root = os.path.normcase(str(ws.root.parent.resolve()))
        bound_root = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        if bound_root and bound_root['value'] != root:
            raise StudyError('copied ledger requires a new workspace; authorization is not inherited')
        db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace',?)", (root,))
        yield db, ws
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def enabled(cwd):
    path = Path(cwd) / '.k12' / 'study-sync.sqlite'
    if not path.exists():
        return False
    with sqlite3.connect(path) as db:
        return db.execute("SELECT 1 FROM settings WHERE key='enabled' AND value='true'").fetchone() is not None

def capture(args):
    responses = []
    with database(args) as (db, ws):
        db.execute("INSERT OR REPLACE INTO settings VALUES ('enabled','true')")
        scan = args.get('scan')
        if scan is not None:
            prior = db.execute('SELECT * FROM host_scans WHERE session=?',(ws.session,)).fetchone()
            complete = (scan.get('complete') is True and type(scan.get('throughEventSeq')) is int
                        and scan['throughEventSeq'] >= -1 and isinstance(scan.get('hazards'),list))
            hazards = sorted(set((json.loads(prior['hazards']) if prior else []) + scan.get('hazards', [])))
            complete = complete and (not prior or bool(prior['complete']))
            through = scan.get('throughEventSeq', -1)
            if prior and through < prior['through_seq']:
                complete = False
            db.execute('INSERT OR REPLACE INTO host_scans VALUES (?,?,?,?)',
                       (ws.session,through,int(complete),packed(hazards)))
        for item in args.get('envelopes', []):
            if not isinstance(item.get('messageId'), str) or not item['messageId'] or item.get('role') not in ('user','assistant') or not isinstance(item.get('text'), str):
                raise StudyError('invalid host evidence')
            seq = item.get('eventSeq')
            if seq is not None and (type(seq) is not int or seq < 0):
                raise StudyError('invalid host event sequence')
            timestamp = item.get('attemptedAt')
            if timestamp is not None:
                try:
                    parsed = dt.datetime.fromisoformat(timestamp.replace('Z','+00:00'))
                    if parsed.tzinfo is None:
                        raise ValueError()
                    timestamp = parsed.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')
                except (TypeError, ValueError):
                    raise StudyError('invalid host event time')
            old = db.execute('SELECT * FROM evidence WHERE session=? AND message_id=?', (ws.session,item['messageId'])).fetchone()
            text_hash = digest(item['text'])
            if old:
                if old['hash'] != text_hash or old['role'] != item['role']:
                    db.execute("UPDATE evidence SET coverage='incomplete' WHERE ref=?",(old['ref'],))
                    db.execute('UPDATE host_scans SET complete=0 WHERE session=?',(ws.session,))
                    continue
                if old['seq'] is not None and seq is not None and old['seq'] != seq:
                    db.execute("UPDATE evidence SET coverage='incomplete' WHERE ref=?",(old['ref'],))
                    db.execute('UPDATE host_scans SET complete=0 WHERE session=?',(ws.session,))
                    continue
                if old['attempted_at'] is not None and timestamp is not None and old['attempted_at'] != timestamp:
                    db.execute("UPDATE evidence SET coverage='incomplete' WHERE ref=?",(old['ref'],))
                    db.execute('UPDATE host_scans SET complete=0 WHERE session=?',(ws.session,))
                    continue
                if old['seq'] is None and seq is not None:
                    coverage = 'complete' if db.execute('SELECT 1 FROM pending_metadata WHERE ref=?',(old['ref'],)).fetchone() and timestamp and item.get('coverage') == 'complete' and not item.get('interrupted') else 'incomplete'
                    db.execute('UPDATE evidence SET seq=?,attempted_at=?,coverage=? WHERE ref=?', (seq,timestamp,coverage,old['ref']))
                if item.get('coverage') != 'complete' and seq is not None or item.get('interrupted'):
                    db.execute("UPDATE evidence SET coverage='incomplete' WHERE ref=?",(old['ref'],))
                    db.execute('DELETE FROM pending_metadata WHERE ref=?',(old['ref'],))
                ref = old['ref']
            else:
                ref = uid()
                coverage = 'complete' if seq is not None and timestamp is not None and item.get('coverage') == 'complete' and not item.get('interrupted') else 'incomplete'
                earlier = seq is not None and db.execute('SELECT 1 FROM evidence WHERE session=? AND seq>?', (ws.session,seq)).fetchone()
                if earlier:
                    coverage = 'incomplete'
                    db.execute("UPDATE evidence SET coverage='incomplete' WHERE session=?", (ws.session,))
                db.execute('INSERT INTO evidence(ref,session,message_id,role,text,hash,seq,attempted_at,captured_at,coverage) VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (ref,ws.session,item['messageId'],item['role'],item['text'],text_hash,seq,timestamp,now(),coverage))
                if item.get('pendingCommit') is True and seq is None and timestamp is None:
                    db.execute('INSERT OR IGNORE INTO pending_metadata VALUES (?)',(ref,))
            if item['role'] == 'user':
                if not old or not old['dismissed']:
                    responses.append({'responseRef':ref,'messageId':item['messageId']})
        downgrade_contradictions(db, ws.session)
    return {'responses': responses[-20:], 'observationEnabled': False}

def unattributed_history(db, session, opened_order, opened_seq):
    return db.execute("""SELECT 1 FROM evidence output
      WHERE output.session=? AND output.role='assistant' AND length(trim(output.text))>0
        AND (output.capture_order<=? OR output.seq<=?)
      AND NOT EXISTS (SELECT 1 FROM exercises prior JOIN exercise_windows w ON w.exercise_id=prior.id
        JOIN records first ON first.event_id=prior.first_whole
        JOIN evidence response ON response.ref=first.response_ref
        WHERE prior.session=output.session AND w.fingerprint IS NOT NULL
          AND output.capture_order>prior.opened_order AND output.capture_order<response.capture_order
          AND output.coverage='complete' AND output.text=json_extract(prior.snapshot,'$.question.stem'))
      LIMIT 1""",(session,opened_order,opened_seq)).fetchone()


def earlier_learner_input(db, session, question, response):
    # All captured learner inputs count, including dismissed inputs and other answer slots.
    return db.execute("""SELECT 1 FROM evidence WHERE session=? AND role='user' AND ref<>?
      AND ((seq>? AND seq<?) OR (capture_order>? AND capture_order<?)) LIMIT 1""",
      (session,response['ref'],question['seq'],response['seq'],
       question['capture_order'],response['capture_order'])).fetchone()


def downgrade_contradictions(db, session):
    """Append a help revision; never rewrite a frozen create/retry payload."""
    scan = db.execute('SELECT * FROM host_scans WHERE session=?',(session,)).fetchone()
    rows = db.execute("SELECT r.*,e.opened_order FROM records r JOIN exercises e ON e.id=r.exercise_id WHERE e.session=? AND r.record IS NOT NULL AND r.sync<>'deleted'",(session,)).fetchall()
    for row in rows:
        data = json.loads(row['record'])
        submission = data.get('submission',{})
        proof = submission.get('hostProof')
        if not proof or submission['help']['assistance'] != 'none':
            continue
        evidence = db.execute('SELECT * FROM evidence WHERE ref=?',(row['response_ref'],)).fetchone()
        window = db.execute('SELECT opened_seq FROM exercise_windows WHERE exercise_id=?',(row['exercise_id'],)).fetchone()
        opened_seq = window['opened_seq'] if window else -1
        outputs = db.execute("SELECT * FROM evidence WHERE session=? AND role='assistant' AND seq>? AND seq<?",(session,opened_seq,proof['throughEventSeq'])).fetchall()
        bad = (not scan or not scan['complete'] or not evidence or evidence['coverage'] != 'complete'
               or len(outputs)!=1 or outputs[0]['message_id']!=proof['questionMessageId']
               or outputs[0]['text']!=proof['deliveryText'] or outputs[0]['coverage']!='complete'
               or earlier_learner_input(db,session,outputs[0],evidence)
               or any(seq < proof['throughEventSeq'] for seq in json.loads(scan['hazards']))
               or unattributed_history(db,session,row['opened_order'],opened_seq))
        if not bad:
            continue
        change = {'help':{**submission['help'],'assistance':'unknown','coverage':'incomplete'}}
        submission.update(change)
        previous = db.execute('SELECT * FROM outbox WHERE event_id=? ORDER BY local_revision DESC LIMIT 1',(row['event_id'],)).fetchone()
        revision = row['local_revision'] + 1
        dependency = previous['id'] if previous and previous['state'] in ('pending','blocked','conflict') else None
        db.execute('UPDATE records SET record=?,local_revision=?,sync=? WHERE event_id=?',(packed(data),revision,'pending',row['event_id']))
        operation_id = uid()
        db.execute('INSERT INTO outbox(id,event_id,local_revision,operation,intent,binding,base_revision,dependency,state) VALUES (?,?,?,?,?,?,?,?,?)',
                   (operation_id,row['event_id'],revision,'update',packed(change),previous['binding'] if previous else None,row['canonical_revision'],dependency,'pending'))
        db.execute('INSERT INTO safety_revisions VALUES (?)',(operation_id,))


def fields(args, allowed):
    if set(args) - set(allowed) - {'cwd','session'}:
        raise StudyError('unknown command field')

def judgment(value):
    if not isinstance(value,dict):
        raise StudyError('judgment required')
    if value.get('state') == 'pending' and set(value) == {'state','reason'} and value['reason'] in ('technical','incomplete','needs_review'):
        return value
    if value.get('state') == 'graded' and set(value) == {'state','correct','errorCause'} and type(value['correct']) is bool:
        if value['correct'] and value['errorCause'] is not None:
            raise StudyError('correct answers cannot have error cause')
        if value['errorCause'] not in (None,'concept','method','calc','misread','careless'):
            raise StudyError('invalid error cause')
        return value
    raise StudyError('invalid judgment')

def receipt(db, event):
    row = db.execute('SELECT * FROM records WHERE event_id=?', (event,)).fetchone()
    sync = {'state':row['sync']}
    if row['record_id']:
        sync.update(recordId=row['record_id'], canonicalRevision=row['canonical_revision'])
    return {'eventId':event, 'localRevision':row['local_revision'], 'sync':sync}

def binding_snapshot(cwd):
    from .study_transport import load_binding
    binding = load_binding(str(cwd))
    if not binding:
        return None
    return {key:binding[key] for key in ('studentId','endpoint','bindingVersion')}

def assessment(args):
    """Open a fixed assessment without exposing routing materials in tool cards."""
    fields(args, {'assessment', 'slot', 'purpose'})
    if not enabled(args.get('cwd')):
        raise StudyError('study integration is disabled')
    from .study_templates import build
    from .session import route_task
    try:
        template = build(args.get('assessment'))
    except ValueError as exc:
        raise StudyError(str(exc)) from exc
    if args.get('slot') not in ('diagnose-1','practice-1','transfer-1','review-1','reflection-1') or args.get('purpose') not in ('diagnosis','practice','transfer','review'):
        raise StudyError('invalid assessment slot or purpose')
    context = {key: args.get(key) for key in ('cwd', 'session')}
    routed = route_task({**context, 'action':'start', 'playbook':'practice', 'subject':'math',
                         'grade':8, 'knowledge_points':template['teacherKnowledgeIds']})
    return record({**context, 'action':'open', 'taskRef':routed['task_id'],
                   **{key:args[key] for key in ('assessment','slot','purpose')}})


def record(args):
    action = args.get('action')
    allowed = {
      'open': {'action','taskRef','slot','question','assessment','purpose','follows','taughtBy'},
      'answer': {'action','exerciseId','responseRef','answerSlot','scope','judgment','reportedAssistance'},
      'skip': {'action','exerciseId','responseRef'},
      'dismiss': {'action','responseRef','reason'},
      'revise': {'action','eventId','localRevision','judgment','mappingVersion','reason'},
    }
    if action not in allowed:
        raise StudyError('invalid action')
    fields(args,allowed[action])
    if not enabled(args.get('cwd')):
        raise StudyError('study integration is disabled')
    with database(args) as (db,ws):
        if action == 'open':
            task = next((task for task in ws.state()['tasks'] if args.get('taskRef') in (task['id'],f'{ws.session}:{task["id"]}')),None)
            if not task:
                raise StudyError('taskRef must reference this session task')
            if args.get('slot') not in ('diagnose-1','practice-1','transfer-1','review-1','reflection-1'):
                raise StudyError('slot must be a predefined exercise slot')
            if args.get('purpose') not in ('diagnosis','practice','transfer','review'):
                raise StudyError('invalid purpose')
            template = None
            if 'assessment' in args:
                if 'question' in args:
                    raise StudyError('choose question or assessment')
                from .study_templates import build
                try:
                    template = build(args['assessment'])
                except ValueError as exc:
                    raise StudyError(str(exc)) from exc
                if not set(template['teacherKnowledgeIds']).issubset(task['knowledge_points']):
                    raise StudyError('assessment must match routed knowledge point')
            question = template['question'] if template else args.get('question')
            if not isinstance(question,dict) or set(question)-{'type','stem','options','questionId','referenceAnswer'} or not isinstance(question.get('stem'),str) or not question['stem'].strip():
                raise StudyError('confirmed question required')
            question = {**question,'options':question.get('options'),'referenceAnswer':question.get('referenceAnswer')}
            if question.get('type') not in ('single','multiple','blank','free') or len(question['stem'])>4000:
                raise StudyError('invalid question type or length')
            options = question['options']
            if (question['type'] in ('single','multiple')) != (options is not None):
                raise StudyError('choice question requires options; other question types forbid options')
            if options is not None and (not isinstance(options,list) or not 2<=len(options)<=10 or any(not isinstance(o,dict) or set(o)!={'key','text'} or not isinstance(o['key'],str) or not isinstance(o['text'],str) for o in options)):
                raise StudyError('invalid question options')
            if question['referenceAnswer'] is not None and (not isinstance(question['referenceAnswer'],str) or len(question['referenceAnswer'])>8000):
                raise StudyError('invalid reference answer')
            snapshot = {'taskRef':f'{ws.session}:{task["id"]}', 'slot':args['slot'], 'purpose':args['purpose'], 'question':question,'snapshotHash':digest(packed({k:v for k,v in question.items() if k!='referenceAnswer'}))}
            if template:
                snapshot['assessment'] = args['assessment']
            for key in ('follows','taughtBy'):
                if args.get(key):
                    if key == 'follows' and not db.execute('SELECT 1 FROM exercises WHERE id=? AND session=?',(args[key],ws.session)).fetchone():
                        raise StudyError('unknown follows exercise')
                    if key == 'taughtBy' and not any(a.get('activityId') == args[key] for a in ws.state().get('activities',[])):
                        raise StudyError('unknown teaching activity')
                    snapshot[key] = args[key]
            old = db.execute('SELECT * FROM exercises WHERE session=? AND task=? AND slot=?',(ws.session,task['id'],args['slot'])).fetchone()
            if old:
                previous = json.loads(old['snapshot'])
                if {k:v for k,v in previous.items() if k!='exerciseId'} != snapshot:
                    raise StudyError('exercise slot snapshot conflict')
                return {'exerciseId':old['id'],'answerSlots':['whole','reflection','step-1'], **({'deliveryText':template['deliveryText']} if template else {})}
            exercise_id = uid()
            snapshot['exerciseId'] = exercise_id
            order = db.execute('SELECT coalesce(max(capture_order),0) FROM evidence').fetchone()[0]
            db.execute('INSERT INTO exercises VALUES (?,?,?,?,?,?,NULL)',(exercise_id,ws.session,task['id'],args['slot'],packed(snapshot),order))
            scan = db.execute('SELECT * FROM host_scans WHERE session=?',(ws.session,)).fetchone()
            fingerprint = template['fingerprint'] if template else None
            repeated = fingerprint and db.execute('SELECT 1 FROM seen_assessments WHERE fingerprint=?',(fingerprint,)).fetchone()
            # Only closed prior assessment windows attribute historical assistant output.
            # Opening a decoy template must never erase an unassigned explanation.
            taint_key = 'host-taint:' + ws.session
            prior_unattributed = db.execute('SELECT 1 FROM settings WHERE key=?',(taint_key,)).fetchone()
            if not prior_unattributed:
                prior_unattributed = unattributed_history(db,ws.session,order,scan['through_seq'] if scan else -1)
            prior_unattributed = prior_unattributed or (scan and bool(json.loads(scan['hazards'])))
            if prior_unattributed:
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)',(taint_key,'true'))
            db.execute('INSERT INTO exercise_windows VALUES (?,?,?,?)',
                       (exercise_id,scan['through_seq'] if scan else None,fingerprint,int(bool(scan and scan['complete'] and not repeated and not prior_unattributed))))
            if fingerprint:
                db.execute('INSERT OR IGNORE INTO seen_assessments VALUES (?,?)',(ws.session,fingerprint))
            return {'exerciseId':exercise_id,'answerSlots':['whole','reflection','step-1'], **({'deliveryText':template['deliveryText']} if template else {})}
        if action == 'revise':
            row = db.execute('SELECT r.* FROM records r JOIN exercises e ON e.id=r.exercise_id WHERE event_id=? AND e.session=?',(args.get('eventId'),ws.session)).fetchone()
            if not row or row['sync']=='deleted':
                raise StudyError('stale or missing local revision')
            if not args.get('reason'):
                raise StudyError('revision reason required')
            change = {'judgment':judgment(args['judgment'])} if 'judgment' in args else {}
            data = json.loads(row['record'])
            if 'mappingVersion' in args:
                from .study_templates import build, VERSION
                frozen = json.loads(db.execute('SELECT snapshot FROM exercises WHERE id=?',(row['exercise_id'],)).fetchone()[0])
                if args['mappingVersion'] != VERSION or not frozen.get('assessment'):
                    raise StudyError('unknown mapping version or assessment')
                mapped = build(frozen['assessment'])
                change.update({key:mapped[key] for key in ('teacherKnowledgeIds','knowledgeIds','mappingVersion')})
            if not change:
                raise StudyError('revision change required')
            if data['kind'] != 'answer':
                raise StudyError('skip cannot be graded')
            if data['submission'].get('hostProof') and data['submission']['help']['assistance'] != 'none':
                change['help'] = data['submission']['help']
            if row['sync'] != 'conflict' and all(data['submission'].get(key) == value for key,value in change.items()) and args.get('localRevision') in (row['local_revision'],row['local_revision']-1):
                return receipt(db,row['event_id'])
            if row['local_revision'] != args.get('localRevision'):
                raise StudyError('stale local revision')
            if data['submission']['evidence']['completeness'] != 'complete' and change.get('judgment',{}).get('state') == 'graded':
                raise StudyError('incomplete evidence requires pending judgment')
            data['submission'].update(change)
            previous = db.execute('SELECT * FROM outbox WHERE event_id=? ORDER BY local_revision DESC LIMIT 1',(row['event_id'],)).fetchone()
            dependency = previous['id'] if previous and previous['state'] in ('pending','blocked') else None
            if previous and previous['state'] == 'conflict':
                db.execute("UPDATE outbox SET state='superseded' WHERE event_id=? AND state='conflict'",(row['event_id'],))
            binding = previous['binding'] if previous else None
            revision = row['local_revision']+1
            db.execute('UPDATE records SET record=?,local_revision=?,sync=? WHERE event_id=?',(packed(data),revision,'pending',row['event_id']))
            db.execute('INSERT INTO outbox(id,event_id,local_revision,operation,intent,binding,base_revision,dependency,state) VALUES (?,?,?,?,?,?,?,?,?)',
                       (uid(),row['event_id'],revision,'update',packed(change),binding,row['canonical_revision'],dependency,'pending'))
            return receipt(db,row['event_id'])
        evidence = db.execute('SELECT * FROM evidence WHERE ref=? AND session=? AND role=?',(args.get('responseRef'),ws.session,'user')).fetchone()
        if not evidence:
            raise StudyError('responseRef must reference captured user input in this session')
        if action == 'dismiss':
            if args.get('reason') not in ('self_report','not_answer','context','technical'):
                raise StudyError('invalid dismissal reason')
            db.execute('UPDATE evidence SET dismissed=? WHERE ref=?',(args['reason'],evidence['ref']))
            return {'dismissed':evidence['ref']}
        exercise = db.execute('SELECT * FROM exercises WHERE id=? AND session=?',(args.get('exerciseId'),ws.session)).fetchone()
        if not exercise:
            raise StudyError('exercise must belong to this session')
        if evidence['capture_order'] <= exercise['opened_order']:
            raise StudyError('response must follow exercise opening')
        slot = args.get('answerSlot') if action == 'answer' else 'skip'
        if action == 'answer':
            scope = args.get('scope')
            expected = {'whole':{'kind':'whole'},'reflection':{'kind':'reflection'},'step-1':{'kind':'step','stepKey':'step-1'}}
            if slot not in expected or scope != expected[slot]:
                raise StudyError('invalid answer slot or scope')
            if not evidence['text'].strip() or len(evidence['text']) > 16000:
                raise StudyError('answer evidence is empty or exceeds supported bound')
            decision = judgment(args.get('judgment'))
            if evidence['coverage']=='incomplete' and decision['state']=='graded':
                raise StudyError('incomplete evidence requires pending judgment')
        old = db.execute('SELECT * FROM records WHERE exercise_id=? AND response_ref=? AND answer_slot=?',(exercise['id'],evidence['ref'],slot)).fetchone()
        if old:
            if action == 'answer' and json.loads(old['record'])['submission']['judgment'] != decision:
                raise StudyError('use revise to change a judgment')
            return receipt(db,old['event_id'])
        event = uid()
        if action == 'skip':
            if not evidence['attempted_at'] or not any(word in evidence['text'].lower() for word in ('跳过','不做','结束','skip','stop')):
                raise StudyError('skip needs a captured explicit skip with host time')
            data = {'kind':'skip','eventId':event,'exerciseId':exercise['id'],'reasonRef':evidence['ref'],'confirmedAt':evidence['attempted_at']}
        else:
            assistance = args.get('reportedAssistance','unknown')
            if assistance not in ('none','hint','revealed','unknown'):
                raise StudyError('invalid reported assistance')
            assistance = assistance if assistance in ('hint','revealed') else 'unknown'
            help_rows = db.execute("SELECT * FROM evidence WHERE session=? AND role='assistant' AND capture_order>? AND capture_order<?",(ws.session,exercise['opened_order'],evidence['capture_order'])).fetchall()
            help_snapshot = {'assistance':assistance,'coverage':'incomplete','window':{'fromCaptureOrder':exercise['opened_order'],'toCaptureOrder':evidence['capture_order']},'evidence':[{'sourceRef':r['ref'],'kind':'unknown','fact':'assistant_output_recorded','attribution':'unknown'} for r in help_rows[-100:]]}
            frozen = json.loads(exercise['snapshot'])
            template = None
            proof = None
            if frozen.get('assessment'):
                from .study_templates import build
                template = build(frozen['assessment'])
                window = db.execute('SELECT * FROM exercise_windows WHERE exercise_id=?',(exercise['id'],)).fetchone()
                scan = db.execute('SELECT * FROM host_scans WHERE session=?',(ws.session,)).fetchone()
                delivered = len(help_rows) == 1 and help_rows[0]['text'] == template['deliveryText'] and help_rows[0]['coverage'] == 'complete'
                clean = (window and window['clean'] and scan and scan['complete'] and evidence['coverage']=='complete'
                         and evidence['seq'] is not None and scan['through_seq'] >= evidence['seq']
                         and delivered and window['opened_seq'] < help_rows[0]['seq'] < evidence['seq']
                         and not earlier_learner_input(db,ws.session,help_rows[0],evidence)
                         and not any(window['opened_seq'] < seq < evidence['seq'] for seq in json.loads(scan['hazards']))
                         and exercise['first_whole'] is None and slot == 'whole')
                if clean and assistance not in ('hint','revealed'):
                    help_snapshot.update(assistance='none',coverage='complete',evidence=[])
                    proof = {'version':'host-window/v1','sessionId':ws.session,'questionMessageId':help_rows[0]['message_id'],
                             'responseMessageId':evidence['message_id'],'fromEventSeq':help_rows[0]['seq'],
                             'throughEventSeq':evidence['seq'],'complete':True,'deliveryText':template['deliveryText']}
            response = {'responseRef':evidence['ref'],'messageId':evidence['message_id'],'sessionId':ws.session,'eventSeq':evidence['seq'],'attemptedAt':evidence['attempted_at'],'capturedAt':evidence['captured_at'],'captureOrder':evidence['capture_order'],'textHash':evidence['hash'],'selectedText':evidence['text'],'source':'chat','completeness':evidence['coverage']}
            data = {'kind':'answer','submission':{'eventId':event,'exerciseId':exercise['id'],'scope':scope,'evidence':response,'judgment':decision,'help':help_snapshot,'teacherKnowledgeIds':[],'knowledgeIds':[],'mappingVersion':None}}
            if template:
                data['submission'].update({k:template[k] for k in ('teacherKnowledgeIds','knowledgeIds','mappingVersion')})
            if proof:
                data['submission']['hostProof'] = proof
            if slot == 'whole' and exercise['first_whole'] is None:
                db.execute('UPDATE exercises SET first_whole=? WHERE id=?',(event,exercise['id']))
        snapshot = binding_snapshot(ws.root.parent)
        wire = {'exercise':json.loads(exercise['snapshot']),'record':data}
        db.execute('INSERT INTO records VALUES (?,?,?,?,?,1,NULL,NULL,?)',(event,exercise['id'],evidence['ref'],slot,packed(data),'pending'))
        db.execute('INSERT INTO outbox(id,event_id,local_revision,operation,intent,binding,state) VALUES (?,?,1,?,?,?,?)',(uid(),event,'create',packed(wire),packed(snapshot) if snapshot else None,'pending'))
        return receipt(db,event)

def apply_remote(db, view):
    tutor = view['tutor']
    event = tutor['eventId']
    local = db.execute('SELECT * FROM records WHERE event_id=?',(event,)).fetchone()
    if not local:
        return
    if local['sync'] == 'deleted' or (local['canonical_revision'] is not None and tutor['revision'] < local['canonical_revision']):
        return
    if tutor['deletedAt']:
        db.execute("UPDATE records SET record=NULL,sync='deleted',canonical_revision=?,record_id=? WHERE event_id=?",(tutor['revision'],tutor['recordId'],event))
        db.execute("UPDATE outbox SET state='deleted',intent='{}',wire=NULL WHERE event_id=?",(event,))
        db.execute('DELETE FROM cache')
        db.execute("UPDATE evidence SET text='' WHERE ref=?",(local['response_ref'],))
        return
    pending = db.execute("SELECT * FROM outbox WHERE event_id=? AND state IN ('pending','conflict','blocked')",(event,)).fetchall()
    conflicted = any(row['state']=='conflict' for row in pending) or any(row['operation']=='update' and row['wire'] is None and row['dependency'] is None and row['base_revision'] != tutor['revision'] for row in pending)
    if conflicted:
        db.execute("UPDATE outbox SET state='conflict',reason='remote revision changed' WHERE event_id=? AND state='pending'",(event,))
    # A stale safety-only correction gets a NEW mutation based on this exact read.
    # Ordinary requested revisions retain the original conflict behavior.
    if conflicted and local['record']:
        safety = db.execute("""SELECT o.* FROM outbox o JOIN safety_revisions s ON s.outbox_id=o.id
          WHERE o.event_id=? AND o.state='conflict' ORDER BY o.local_revision DESC LIMIT 1""",(event,)).fetchone()
        remote_submission = tutor.get('record',{}).get('submission',{})
        if safety and remote_submission.get('help',{}).get('assistance') == 'none':
            change = json.loads(safety['intent'])
            if set(change) == {'help'} and change['help']['assistance'] == 'unknown':
                revision = local['local_revision'] + 1
                operation_id = uid()
                db.execute("UPDATE outbox SET state='superseded' WHERE id=?",(safety['id'],))
                db.execute('INSERT INTO outbox(id,event_id,local_revision,operation,intent,binding,base_revision,state) VALUES (?,?,?,?,?,?,?,?)',
                           (operation_id,event,revision,'update',safety['intent'],safety['binding'],tutor['revision'],'pending'))
                db.execute('INSERT INTO safety_revisions VALUES (?)',(operation_id,))
                db.execute('UPDATE records SET local_revision=? WHERE event_id=?',(revision,event))
                pending = db.execute("SELECT * FROM outbox WHERE event_id=? AND state IN ('pending','conflict','blocked')",(event,)).fetchall()
                conflicted = any(item['state']=='conflict' for item in pending)
    db.execute('UPDATE records SET canonical_revision=?,record_id=?,sync=? WHERE event_id=?',
               (tutor['revision'],tutor['recordId'],'conflict' if conflicted else ('pending' if pending else 'synced'),event))
    if not pending:
        advanced = local['canonical_revision'] is not None and tutor['revision'] != local['canonical_revision']
        db.execute('UPDATE records SET record=?,local_revision=local_revision+? WHERE event_id=?',(packed(tutor['record']),int(advanced),event))

def reconcile(args, transport):
    with database(args) as (db,ws):
        row = db.execute("SELECT value FROM settings WHERE key='cursor'").fetchone()
        cursor = row['value'] if row else '0'
    page = transport.call('changes',cursor=cursor,limit=100)
    for change in page['changes']:
        view = transport.call('detail',record_id=change['recordId'])
        with database(args) as (db,ws):
            apply_remote(db,view)
    with database(args) as (db,ws):
        previous = db.execute("SELECT value FROM settings WHERE key='cursor'").fetchone()
        if previous is None or int(page['cursor']) >= int(previous['value']):
            db.execute("INSERT OR REPLACE INTO settings VALUES ('cursor',?)",(page['cursor'],))

def flush(args, limit=20, transport=None):
    from .study_transport import load_binding, Transport, TransportError
    binding = load_binding(args['cwd'])
    if not binding:
        return {'state':'blocked','reason':'workspace unbound'}
    target = {key:binding[key] for key in ('studentId','endpoint','bindingVersion')}
    try:
        transport = transport or Transport(binding)
        reconcile(args,transport)
    except TransportError as exc:
        return {'state':exc.state,'reason':exc.reason}
    with database(args) as (db,ws):
        db.execute("UPDATE outbox SET state='pending' WHERE state='blocked' AND reason IN ('credentials invalid','credential reference is unavailable','CLI installation changed; verify binding again')")
    sent = 0
    for _ in range(min(limit,100)):
        with database(args) as (db,ws):
            row = db.execute("SELECT * FROM outbox WHERE state='pending' AND lease_until<? AND (dependency IS NULL OR EXISTS (SELECT 1 FROM outbox predecessor WHERE predecessor.id=outbox.dependency AND predecessor.state='synced')) ORDER BY rowid LIMIT 1",(time.time(),)).fetchone()
            if not row:
                break
            original_target = json.loads(row['binding']) if row['binding'] else None
            if not original_target or any(original_target.get(key) != target[key] for key in ('studentId','endpoint')) or original_target['bindingVersion'] > target['bindingVersion']:
                db.execute("UPDATE outbox SET state='blocked',reason='binding mismatch or unbound origin' WHERE id=?",(row['id'],))
                db.execute("UPDATE records SET sync='blocked' WHERE event_id=?",(row['event_id'],))
                continue
            current = db.execute('SELECT * FROM records WHERE event_id=?',(row['event_id'],)).fetchone()
            base = row['base_revision']
            if row['dependency']:
                dep = db.execute('SELECT * FROM outbox WHERE id=?',(row['dependency'],)).fetchone()
                if dep['state'] != 'synced':
                    break
                base = json.loads(dep['receipt'])['appliedRevision']
            if row['wire']:
                payload = json.loads(row['wire'])
            else:
                payload = {**json.loads(row['intent']),'mutationId':row['id']}
                if row['operation']=='update':
                    if not base or current['canonical_revision'] != base:
                        db.execute("UPDATE outbox SET state='conflict',reason='revision changed before send' WHERE event_id=? AND state='pending'",(row['event_id'],))
                        db.execute("UPDATE records SET sync='conflict' WHERE event_id=?",(row['event_id'],))
                        continue
                    payload['expectedRevision'] = base
                db.execute('UPDATE outbox SET wire=? WHERE id=?',(packed(payload),row['id']))
            db.execute('UPDATE outbox SET lease_until=? WHERE id=?',(time.time()+120,row['id']))
        try:
            result = transport.call(row['operation'],payload=payload,record_id=current['record_id'])
            if result.get('eventId') != row['event_id'] or result.get('mutationId') != row['id']:
                raise TransportError('blocked','invalid remote receipt')
            view = transport.call('detail',record_id=result['recordId'])
            with database(args) as (db,ws):
                db.execute("UPDATE outbox SET state='synced',receipt=?,lease_until=0 WHERE id=? AND state<>'deleted'",(packed(result),row['id']))
                apply_remote(db,view)
            sent += 1
        except TransportError as exc:
            with database(args) as (db,ws):
                db.execute('UPDATE outbox SET state=?,reason=?,lease_until=0 WHERE id=?',(exc.state,exc.reason,row['id']))
                db.execute('UPDATE records SET sync=? WHERE event_id=? AND sync<>?',(exc.state,row['event_id'],'deleted'))
                if exc.state == 'conflict':
                    db.execute("UPDATE outbox SET state='conflict',reason='predecessor conflict' WHERE event_id=? AND state='pending'",(row['event_id'],))
            break
    return {'state':'complete','sent':sent}

def query(args):
    fields(args,{'kind','limit','cursor','knowledgeId','exerciseId'})
    kind = args.get('kind','recent')
    limit = args.get('limit',20)
    if kind not in ('recent','wrong','mastery','gaps') or type(limit) is not int or not 1<=limit<=100:
        raise StudyError('invalid bounded query')
    if not enabled(args.get('cwd')):
        return {'freshness':'unavailable','reason':'disabled','observedAt':None}
    from .study_transport import load_binding, Transport, TransportError
    binding = load_binding(args['cwd'])
    if not binding:
        return {'freshness':'unavailable','reason':'unbound','observedAt':None}
    key = packed([binding['studentId'],binding['endpoint'],kind,limit,args.get('cursor'),args.get('knowledgeId'),args.get('exerciseId')])
    sync = {'state':'blocked','reason':'credentials unavailable'}
    try:
        transport = Transport(binding)
        sync = flush(args,transport=transport)
        value = transport.call(kind,limit=limit,cursor=args.get('cursor'),knowledge_id=args.get('knowledgeId'),exercise_id=args.get('exerciseId'))
        stamp = now()
        with database(args) as (db,ws):
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)',(key,packed(value),stamp))
            local_receipts = [receipt(db,row['event_id']) for row in db.execute('SELECT event_id FROM records ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()]
        return {'freshness':'live','observedAt':stamp,'data':value,'localReceipts':local_receipts,'sync':sync,'observationEnabled':False}
    except TransportError:
        with database(args) as (db,ws):
            row = db.execute('SELECT * FROM cache WHERE key=?',(key,)).fetchone()
        if row:
            return {'freshness':'cached','observedAt':row['observed_at'],'data':json.loads(row['value']),'sync':sync}
        return {'freshness':'unavailable','observedAt':None,'sync':sync}
