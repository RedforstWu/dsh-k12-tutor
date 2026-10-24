from __future__ import annotations

import json
import re

from . import route


def protection(question, context='unknown', *, source='unknown', evidence_refs=None, policy=None):
    from .session import solve_problem
    problem = question.get('stem') or ''
    effective = ('refuse' if context == 'exam-live' or policy == 'refuse' else
                 'withhold' if context == 'homework' else policy or 'withhold')
    return {'answer_policy': effective,
            'assessment_context': context, 'context_source': source, 'context_refs': evidence_refs or [],
            'problem': problem, 'answers': [], 'problem_answers': solve_problem(problem),
            'student_texts': [], 'reveal_authorized': False}


def ensure(db, row, legacy=None):
    found = db.execute('SELECT value FROM exercise_state WHERE exercise_id=?', (row['id'],)).fetchone()
    if found:
        return json.loads(found['value'])
    snapshot = json.loads(row['snapshot'])
    legacy = legacy or {}
    value = protection(snapshot['question'], legacy.get('assessment_context', 'unknown'),
                       source='legacy' if legacy else 'unknown', policy=legacy.get('answer_policy'))
    for key in ('answers', 'problem_answers', 'student_texts'):
        if key in legacy:
            value[key] = legacy[key]
    if legacy.get('revealed') and value['assessment_context'] not in ('homework', 'exam-live') and value['answer_policy'] != 'refuse':
        value['answer_policy'] = 'revealed'
        value['reveal_authorized'] = True
    save(db, row['id'], value)
    return value


def save(db, exercise_id, value):
    from .study import packed
    db.execute('INSERT OR REPLACE INTO exercise_state VALUES (?,?)', (exercise_id, packed(value)))


def context_exercise(db, ws, task_id, legacy, source='context'):
    from .study import digest, packed, uid
    existing = db.execute('SELECT * FROM exercises WHERE session=? AND task=? AND slot=?',
                          (ws.session, task_id, 'context-1')).fetchone()
    if existing:
        ensure(db, existing, legacy)
        return existing['id']
    question = {'type': 'free', 'stem': legacy.get('problem') or legacy.get('topic') or '受保护学习情境',
                'options': None, 'referenceAnswer': None}
    exercise_id = uid()
    snapshot = {'exerciseId': exercise_id, 'taskRef': f'{ws.session}:{task_id}', 'slot': 'context-1',
                'question': question, 'purpose': 'diagnosis', 'source': source,
                'snapshotHash': digest(packed({k: v for k, v in question.items() if k != 'referenceAnswer'}))}
    order = db.execute('SELECT coalesce(max(capture_order),0) FROM evidence').fetchone()[0]
    db.execute('INSERT INTO exercises VALUES (?,?,?,?,?,?,NULL)',
               (exercise_id, ws.session, task_id, 'context-1', packed(snapshot), order))
    row = db.execute('SELECT * FROM exercises WHERE id=?', (exercise_id,)).fetchone()
    value = ensure(db, row, legacy)
    value['context_source'] = source if source == 'legacy' else 'unverified_context'
    value['context_only'] = not bool(legacy.get('problem'))
    value = retain_question_constraints(db, ws.session, question, value)
    save(db, exercise_id, value)
    db.execute('INSERT INTO exercise_windows VALUES (?,NULL,NULL,0)', (exercise_id,))
    return exercise_id


def read(ws, exercise_id, task_id=None):
    from .study import database
    flags = ws.state().get('flags', [])
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        row = db.execute('SELECT * FROM exercises WHERE id=? AND session=?', (exercise_id, ws.session)).fetchone()
        if not row or (task_id is not None and row['task'] != task_id):
            return None
        value = tighten(db, row, flags)
        return {'exerciseId': row['id'], 'taskRef': f'{ws.session}:{row["task"]}',
                'snapshot': json.loads(row['snapshot']), **value}


def task_view(ws, task, exercise_id=None):
    if task is None:
        return None
    reference = exercise_id or task.get('activeExerciseId')
    value = read(ws, reference, task['id']) if reference else None
    result = {**task, 'playbook': task['guidance']['playbook'], 'answer_policy': 'open', 'answers': [],
              'student_texts': [], 'problem': '', 'problem_answers': []}
    if task.get('recoveryRequired') or reference and value is None:
        result.update(answer_policy='refuse', recovery_required=True)
    elif value:
        result.update(value)
    return result


def summary(ws, exercise_id):
    from .study import database
    value = read(ws, exercise_id) if exercise_id else None
    if value is None:
        return None
    question = {key: item for key, item in value['snapshot']['question'].items() if key != 'referenceAnswer'}
    recent = []
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        rows = db.execute('SELECT * FROM records WHERE exercise_id=? ORDER BY rowid DESC LIMIT 5', (exercise_id,)).fetchall()
        for row in reversed(rows):
            data = json.loads(row['record']) if row['record'] else {}
            submission = data.get('submission', {})
            recent.append({'eventId': row['event_id'], 'responseRef': row['response_ref'], 'answerSlot': row['answer_slot'],
                           'localRevision': row['local_revision'], 'kind': data.get('kind'), 'sync': row['sync'],
                           'judgment': submission.get('judgment'), 'help': submission.get('help'),
                           'response': submission.get('evidence', {}).get('selectedText', '')[:500]})
    return {'exerciseId': exercise_id, 'question': question, 'purpose': value['snapshot']['purpose'],
            'source': value['snapshot'].get('source', 'assigned'), 'contextOnly': value.get('context_only', False),
            'answerPolicy': value['answer_policy'], 'assessmentContext': value['assessment_context'],
            'contextSource': value['context_source'], 'revealAuthorized': value['reveal_authorized'], 'recentRecords': recent}


def record_math(ws, exercise_id, answers):
    from .study import database
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        row = db.execute('SELECT * FROM exercises WHERE id=? AND session=?', (exercise_id, ws.session)).fetchone()
        if not row:
            return
        value = ensure(db, row)
        value['answers'] = sorted(set(value['answers']) | set(answers))
        if value['problem'] and not value['problem_answers']:
            value['problem_answers'] = answers
        save(db, exercise_id, value)


def note_input(ws, exercise_id, text, flags):
    from .study import database
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        row = db.execute('SELECT * FROM exercises WHERE id=? AND session=?', (exercise_id, ws.session)).fetchone()
        if not row:
            return
        value = ensure(db, row)
        value['student_texts'] = (value['student_texts'] + [text[:500]])[-100:]
        if any(flag['kind'] == 'exam-live' for flag in flags):
            value.update(answer_policy='refuse', assessment_context='exam-live', context_source='student_input')
        save(db, exercise_id, value)


def reveal(ws, exercise_id, reason, refs):
    from .study import database
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        row = db.execute('SELECT * FROM exercises WHERE id=? AND session=?', (exercise_id, ws.session)).fetchone()
        if not row:
            return {'allowed': False, 'why': '题目引用无法恢复，请显式 resume 原题', 'answer_policy': 'refuse'}
        value = ensure(db, row)
        attempts = (isinstance(refs, list) and bool(refs) and all(isinstance(ref, str) and db.execute('''
            SELECT 1 FROM records r JOIN evidence e ON e.ref=r.response_ref
            WHERE r.exercise_id=? AND r.response_ref=? AND r.record IS NOT NULL
              AND json_extract(r.record,'$.kind')='answer'
              AND json_extract(r.record,'$.submission.evidence.completeness')='complete'
              AND coalesce(json_extract(r.record,'$.submission.judgment.reason'),'') NOT IN ('technical','incomplete')
              AND e.session=? AND e.role='user' AND e.dismissed IS NULL''',
            (exercise_id, ref, ws.session)).fetchone() for ref in refs))
        allowed, why = route.can_reveal(value, bool(reason and reason.strip() and attempts))
        if allowed:
            value.update(answer_policy='revealed', reveal_authorized=True, reveal_reason=reason[:200], reveal_refs=refs)
            save(db, exercise_id, value)
        return {'allowed': allowed, 'why': why, 'answer_policy': value['answer_policy'], 'exerciseId': exercise_id}


def inherited_context(db, task, question):
    if task.get('recoveryRequired'):
        return protection(question, policy='refuse')
    if not task.get('activeExerciseId'):
        return protection(question)
    row = db.execute('SELECT * FROM exercises WHERE id=?', (task['activeExerciseId'],)).fetchone()
    if not row:
        return protection(question, policy='refuse')
    previous = ensure(db, row)
    snapshot = json.loads(row['snapshot'])
    same_question = (previous.get('context_only') or question_key(snapshot['question']) == question_key(question)
                     or snapshot.get('source') in ('context','legacy')
                     and question_text(previous['problem']) == question_text(question['stem']))
    context = previous['assessment_context'] if same_question or previous['assessment_context'] == 'exam-live' else 'unknown'
    return protection(question, context, source=previous['context_source'] if same_question else 'unknown',
                      evidence_refs=previous.get('context_refs') if same_question else None,
                      policy='refuse' if previous['answer_policy'] == 'refuse' else None)


def question_key(question):
    return {'type': question.get('type'), 'stem': question_text(question['stem']),
            'options': [{'key': option['key'], 'text': question_text(option['text'])} for option in question['options']]
                       if question.get('options') is not None else None}


def question_text(text):
    return re.sub(r'\s+', '', text or '')


def retain_question_constraints(db, session, question, value):
    rows = db.execute('SELECT e.* FROM exercises e JOIN exercise_state s ON s.exercise_id=e.id WHERE e.session=?', (session,)).fetchall()
    for row in rows:
        previous = ensure(db, row)
        if previous.get('context_only') or question_key(json.loads(row['snapshot'])['question']) != question_key(question):
            continue
        if previous['answer_policy'] == 'refuse' or previous['assessment_context'] == 'exam-live':
            value.update(answer_policy='refuse', assessment_context=previous['assessment_context'],
                         context_source=previous['context_source'], context_refs=previous.get('context_refs', []))
            break
        if previous['assessment_context'] == 'homework' and value['answer_policy'] != 'refuse':
            value.update(answer_policy='withhold', assessment_context='homework',
                         context_source=previous['context_source'], context_refs=previous.get('context_refs', []))
    return value


def tighten(db, row, flags):
    value = ensure(db, row)
    if any(flag['kind'] == 'exam-live' for flag in flags):
        value.update(answer_policy='refuse', assessment_context='exam-live', context_source='student_input')
        save(db, row['id'], value)
    return value


def reported_context(db, session, value, report):
    from .study import StudyError
    if report is None:
        return value
    if (not isinstance(report, dict) or set(report) != {'value','responseRef'}
            or report['value'] not in route.CONTEXTS):
        raise StudyError('invalid assessmentContext')
    evidence = db.execute("SELECT ref FROM evidence WHERE session=? AND ref=? AND role='user'", (session, report['responseRef'])).fetchone()
    if not evidence:
        raise StudyError('assessmentContext requires a captured student responseRef')
    if value['assessment_context'] == 'exam-live' or value['answer_policy'] == 'refuse':
        return value
    if value['assessment_context'] == 'homework' and report['value'] != 'exam-live':
        return value
    updated = protection({'stem': value['problem']}, report['value'], source='student_report', evidence_refs=[report['responseRef']])
    return {**value, **updated}
