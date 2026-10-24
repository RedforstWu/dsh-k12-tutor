"""Learner profile (``k12-learner/1``): structured facts about one student, never a chat transcript.

The tutor changes it only through validated update ops. Free text (evidence, summaries) is screened for
personal identifiers, so the profile cannot become a store of a minor's contact details.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import re
from contextvars import ContextVar
from pathlib import Path

from . import subjects

SCHEMA = "k12-learner/1"
PREFERENCES = ("pace", "explanation", "encouragement", "language", "notes")
KEEP_OBSERVATIONS = 6
KEEP_SUMMARIES = 12
MAX_TEXT = 300
SOURCE_NAMES = ('teachingProfile', 'mastery', 'gaps')
AVAILABILITY = ('live', 'cached', 'unavailable', 'absent', 'not_requested')
request_context = ContextVar('learner_request_context', default=None)
REFRESH_AFTER = dt.timedelta(minutes=15)
QUALIFIED = re.compile(r"^(?P<subject>[a-z]+)/(?P<stage>[a-z]+):(?P<id>[a-z0-9]+(?:-[a-z0-9]+)*)$")
PII = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "身份证号"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱"),
    (re.compile(r"(住在|家住|住址|地址是|门牌|小区\S{0,6}号楼|微信号|QQ号|qq号)"), "住址或联系方式"),
]


class LearnerError(ValueError):
    pass


def default(learner_id: str) -> dict:
    return {"schema": SCHEMA, "learner_id": learner_id, "nickname": None, "grade": None, "textbooks": {},
            "mastery": {}, "misconceptions": {}, "preferences": {}, "review": [], "summaries": [],
            "observations": [], "updated": None}


def load(path: Path, learner_id: str) -> dict:
    if not path.is_file():
        return default(learner_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise LearnerError(f"{path}: schema must be {SCHEMA}")
    return data


def save(path: Path, profile: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes((json.dumps(profile, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    tmp.replace(path)


def screen(text: str, field: str) -> str:
    text = str(text).strip()
    if not text:
        raise LearnerError(f"{field} must not be empty")
    for pattern, label in PII:
        if pattern.search(text):
            raise LearnerError(f"{field} 含有{label}：学情档案不记录个人身份或联系方式，请删去后再写")
    return text[:MAX_TEXT]


def check_id(qualified: str, kind: str) -> str:
    match = QUALIFIED.match(str(qualified))
    if not match:
        raise LearnerError(f"{kind} id must look like math/junior:linear-eq-solve, got {qualified!r}")
    try:
        p = subjects.pack(match["subject"], match["stage"])
    except subjects.PackError:
        return qualified  # planned subjects have no catalog to check against yet
    table = p.kps if kind == "kp" else p.misconceptions
    if match["id"] not in table:
        raise LearnerError(f"unknown {kind} {qualified}")
    return qualified


EDITION_NAMES = {"人教": "pep-2024", "人教版": "pep-2024", "人民教育出版社": "pep-2024", "pep": "pep-2024"}


def subject_id(name: str) -> str:
    """"数学" and "math" are the same subject; names come from subjects/index.yaml."""
    for entry in subjects.index()["subjects"]:
        if name == entry["id"] or name in str(entry["name"]).replace(" ", "").split("/"):
            return entry["id"]
    return name[:20]


def edition_id(subject: str, edition: str, grade: int | None) -> str:
    """Map what a student says ("人教版") to a pack edition id; free text for subjects without a pack."""
    stage = subjects.stage_of(grade) if grade else "junior"
    try:
        known = subjects.pack(subject, stage).textbooks
    except subjects.PackError:
        return edition[:40]
    if edition in known:
        return edition
    for name, eid in EDITION_NAMES.items():
        if name in edition and eid in known:
            return eid
    raise LearnerError(f"unknown textbook edition {edition!r} for {subject}; known: {', '.join(known)}"
                       "（学生说的版本不在列表里时，记在 preference notes 里，不写 textbooks）")


def apply(profile: dict, updates: list[dict], session: str, now: dt.datetime | None = None) -> tuple[dict, list[str]]:
    """Validate every op first, then apply all of them; one bad op rejects the whole batch."""
    if not isinstance(updates, list) or not updates:
        raise LearnerError("updates must be a non-empty list")
    now = now or dt.datetime.now().astimezone()
    stamp = now.isoformat(timespec="seconds")
    out = copy.deepcopy(profile)
    done = []
    for i, u in enumerate(updates):
        if not isinstance(u, dict):
            raise LearnerError(f"updates[{i}] must be an object")
        op = u.get("op")
        fields = {'basics': {'op', 'grade', 'textbooks', 'nickname'},
                  'observation': {'op', 'kind', 'kp', 'text', 'evidenceRefs'},
                  'preference': {'op', 'key', 'value'}, 'summary': {'op', 'text'}}
        if op not in fields or set(u) - fields[op]:
            raise LearnerError('updates must be basics, observation, preference or summary; answer facts belong in study_record')
        if op == "basics":
            if "grade" in u:
                if not isinstance(u["grade"], int) or not 1 <= u["grade"] <= 12:
                    raise LearnerError("grade must be an integer 1-12")
                out["grade"] = u["grade"]
            for subject, edition in (u.get("textbooks") or {}).items():
                subject = subject_id(str(subject))
                out["textbooks"][subject] = edition_id(subject, str(edition), out.get("grade"))
            if u.get("nickname"):
                out["nickname"] = screen(u["nickname"], "nickname")[:12]
            done.append("basics")
        elif op == "observation":
            refs = u.get('evidenceRefs')
            if u.get('kind') not in ('fact', 'hypothesis') or not isinstance(refs, list) or not 1 <= len(refs) <= 10 or any(not isinstance(ref, str) or not ref for ref in refs):
                raise LearnerError('observation needs kind=fact/hypothesis and ledger evidenceRefs')
            observation = {'at': stamp, 'session': session, 'kind': u['kind'],
                           'text': screen(u.get('text', ''), 'text'), 'evidenceRefs': list(dict.fromkeys(refs))}
            if u.get('kp'):
                observation['kp'] = check_id(u['kp'], 'kp')
            out['observations'] = (out.get('observations', []) + [observation])[-KEEP_OBSERVATIONS:]
            done.append('observation')
        elif op == "preference":
            if u.get("key") not in PREFERENCES:
                raise LearnerError(f"preference key must be one of {', '.join(PREFERENCES)}")
            out["preferences"][u["key"]] = screen(u.get("value", ""), "value")
            done.append(f"preference {u['key']}")
        elif op == "summary":
            out["summaries"].append({"at": stamp, "session": session, "text": screen(u.get("text", ""), "text")})
            out["summaries"] = out["summaries"][-KEEP_SUMMARIES:]
            done.append("summary")
    out["updated"] = stamp
    return out, done


def legacy_binding(args):
    from .study_transport import load_binding
    from .study import digest, packed
    binding = load_binding(args['cwd'])
    if not binding:
        return None
    identity = {'studentId': binding['studentId'], 'endpoint': binding['endpoint']}
    return {'bindingRef': digest(packed([*identity.values(), binding['bindingVersion']])), 'identity': identity}


def authority(ws):
    raw = request_context.get()
    workspace = os.path.normcase(str(ws.root.parent.resolve()))
    if raw is None:
        return {'mode': 'local', 'workspaceRef': workspace, 'bindingRef': None, 'identity': None}
    if (not isinstance(raw, dict) or raw.get('mode') not in ('local', 'legacy', 'family')
            or not isinstance(raw.get('workspaceRef'), str)
            or os.path.normcase(str(Path(raw['workspaceRef']).resolve())) != workspace):
        raise LearnerError('learner context does not match the verified workspace')
    if raw.get('mode') == 'local' and (raw.get('bindingRef') or raw.get('identity')):
        raise LearnerError('local context cannot select a remote identity')
    return raw


def refresh_plan(args):
    from .session import Workspace, current_task
    ws = Workspace(args.get('cwd'), args.get('session'))
    host = authority(ws)
    state = ws.state()
    task = current_task(state)
    previous = state.get('learnerRefresh', {})
    same = (previous.get('bindingRef') == host.get('bindingRef') and previous.get('mode') == host['mode']
            and previous.get('authorityRef') == host.get('authorityRef'))
    attempted = previous.get('attemptedAt')
    stale = not attempted or dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(attempted) >= REFRESH_AFTER
    requested = args.get('event') == 'learner_profile' and args.get('action', 'read') == 'read'
    boundary = requested or args.get('event') == 'route_task'
    missing_cache = host['mode'] == 'family' and attempted and any(
        source.get('availability') == 'not_requested' for source in (host.get('sources') or {}).values())
    refresh = bool(boundary and host['mode'] != 'local' and host.get('bindingRef') and (
        args.get('refresh') is True or requested and (not same or missing_cache or previous.get('dirty'))
        or task and (not same or missing_cache or previous.get('taskRef') != task['taskRef'] or previous.get('dirty')
                     or args.get('event') == 'route_task' and args.get('action') == 'resume' and stale)))
    return {'refresh': refresh, 'taskRef': task['taskRef'] if task else None}


def dirty(args):
    from .session import Workspace
    ws = Workspace(args.get('cwd'), args.get('session'))
    state = ws.state()
    state.setdefault('learnerRefresh', {})['dirty'] = True
    ws.save(state)
    return {'dirty': True}


def _legacy_profile(args, refresh):
    from .study_transport import load_binding, Transport, TransportError
    from .study import database, packed, now
    binding = load_binding(args['cwd'])
    key = packed(['learner-profile', binding['studentId'], binding['endpoint'], binding['bindingVersion']])
    with database(args) as (db, _):
        cached = db.execute('SELECT value,observed_at FROM cache WHERE key=?', (key,)).fetchone()
    previous = json.loads(cached['value']) if cached else None
    if not refresh:
        return ({**previous, 'availability': 'cached'} if previous and 'data' in previous else previous
                or {'availability': 'not_requested', 'observedAt': None})
    try:
        value = Transport(binding)._invoke(['whoami'])
        student = value.get('student') if isinstance(value, dict) else None
        capabilities = value.get('capabilities') if isinstance(value, dict) else None
        if not isinstance(student, dict) or not isinstance(capabilities, dict):
            raise TransportError('blocked', 'invalid learner profile response')
        if student.get('id') != binding['studentId'] or capabilities.get('learningRecordVersion') != 'learning-v1':
            raise TransportError('blocked', 'student identity mismatch')
        data = {'nickname': student.get('displayName') or student.get('name'), 'currentBookId': student.get('currentBookId')}
        stamp = now()
        source = {'availability': 'live', 'observedAt': stamp, 'attemptedAt': stamp, 'data': data}
        with database(args) as (db, _):
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (key, packed(source), stamp))
        return source
    except TransportError as exc:
        reason = 'identity' if 'identity' in exc.reason else 'auth' if exc.code in ('UNAUTHORIZED', 'FORBIDDEN') or 'credential' in exc.reason else 'service'
        stamp = now()
        source = ({**previous, 'availability': 'cached', 'reason': reason, 'attemptedAt': stamp}
                  if previous and 'data' in previous and reason not in ('identity', 'auth')
                  else {'availability': 'unavailable', 'observedAt': None, 'reason': reason, 'attemptedAt': stamp})
        with database(args) as (db, _):
            db.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?)', (key, packed(source), stamp))
        return source


def _legacy_sources(args, refresh):
    from . import study
    profile = _legacy_profile(args, refresh)
    sources = {'teachingProfile': profile}
    for name, kind in (('mastery', 'mastery'), ('gaps', 'gaps')):
        if profile['availability'] == 'not_requested':
            sources[name] = {'availability': 'not_requested', 'observedAt': None}
        elif profile.get('reason') in ('identity', 'auth'):
            sources[name] = {key: value for key, value in profile.items() if key != 'data'}
        else:
            value = study.query({**args, 'kind': kind, 'limit': 20}, refresh=refresh, read_authorized=True)
            sources[name] = {'availability': value['freshness'], 'observedAt': value['observedAt'],
                             **({'data': value['data']} if 'data' in value else {}),
                             **({'attemptedAt': value['attemptedAt']} if value.get('attemptedAt') else {}),
                             **({'reason': value['reason']} if value.get('reason') else {})}
    return sources


def context(ws, state=None, *, event='learner_profile', action='read', refresh=False):
    from .session import current_task
    from .exercise import summary as exercise_summary
    state = state if state is not None else ws.state()
    host = authority(ws)
    args = {'cwd': str(ws.root.parent), 'session': ws.session}
    attempted = host.get('refreshed') is True
    if host['mode'] == 'legacy':
        binding = legacy_binding(args)
        if not binding or binding['bindingRef'] != host.get('bindingRef'):
            raise LearnerError('verified learner binding changed')
        attempted = refresh_plan({**args, 'event': event, 'action': action, 'refresh': refresh})['refresh']
        sources = _legacy_sources(args, attempted)
    elif host['mode'] == 'family':
        sources = copy.deepcopy(host.get('sources') or {})
    else:
        sources = {name: {'availability': 'absent', 'observedAt': None, 'reason': 'local_only'} for name in SOURCE_NAMES}
    for name in SOURCE_NAMES:
        source = sources.setdefault(name, {'availability': 'not_requested', 'observedAt': None})
        if source.get('availability') not in AVAILABILITY or 'observedAt' not in source:
            raise LearnerError('invalid sourced learner context')
        if source['availability'] in ('live', 'cached') and not isinstance(source['observedAt'], str):
            raise LearnerError('available learner source requires an observation time')
    task = current_task(state)
    if attempted:
        state['learnerRefresh'] = {'mode': host['mode'], 'bindingRef': host.get('bindingRef'),
            'authorityRef': host.get('authorityRef'),
            'taskRef': task['taskRef'] if task else None, 'attemptedAt': dt.datetime.now(dt.timezone.utc).isoformat()}
        ws.save(state)
    profile = ws.profile()
    refresh_pending = bool(task and refresh_plan({**args, 'event': 'learner_profile', 'action': 'read'})['refresh'])
    return {'schema': 'k12-learner-context/1', 'mode': host['mode'], 'workspaceRef': os.path.normcase(str(ws.root.parent.resolve())),
            'bindingRef': host.get('bindingRef'), 'identity': host.get('identity'), 'sources': sources,
            'refreshPending': refresh_pending,
            'local': {'profile': {key: profile.get(key) for key in ('nickname', 'grade', 'textbooks', 'updated')},
                      'observations': profile.get('observations', []), 'preferences': profile.get('preferences', {}),
                      'summaries': profile.get('summaries', [])[-2:],
                      'historical': {key: profile.get(key, {}) for key in ('mastery', 'misconceptions', 'review')}},
            'currentSessionEvidence': {'taskRef': task['taskRef'], **(exercise_summary(ws, task.get('activeExerciseId')) or {})} if task else None}


def validate_observations(ws, updates):
    from .study import database
    observations = [value for value in updates if isinstance(value, dict) and value.get('op') == 'observation'] if isinstance(updates, list) else []
    if not observations:
        return
    with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
        for observation in observations:
            refs = observation.get('evidenceRefs')
            if not isinstance(refs, list) or not refs:
                raise LearnerError('observation needs ledger evidenceRefs')
            for reference in refs:
                if not isinstance(reference, str) or not (db.execute("SELECT 1 FROM evidence WHERE ref=? AND role='user'", (reference,)).fetchone()
                    or db.execute("SELECT 1 FROM records WHERE event_id=? AND sync<>'deleted'", (reference,)).fetchone()):
                    raise LearnerError('observation evidenceRefs must reference this workspace ledger')


def summary(view, *, include_history=True):
    local = view['local']
    profile = view['sources']['teachingProfile'].get('data') or {}
    basics = profile if view['bindingRef'] else local['profile']
    lines = [f"模式={view['mode']}；称呼={basics.get('nickname') or '未确认'}；册次={basics.get('currentBookId') or '未确认'}；年级={local['profile'].get('grade') or '未确认'}（本地自述）"]
    if local['profile'].get('textbooks'):
        lines.append('教材版本（本地自述）=' + json.dumps(local['profile']['textbooks'], ensure_ascii=False, separators=(',', ':')))
    if view['refreshPending']:
        lines.append('正式来源待普通教学操作后刷新；以下保留缓存时间或未读取状态。')
    for name in SOURCE_NAMES:
        source = view['sources'][name]
        text = f"{name}: {source['availability']}；时间={source['observedAt'] or '无'}"
        if source.get('reason'):
            text += f"；原因={source['reason']}"
        if source.get('attemptedAt'):
            text += f"；尝试={source['attemptedAt']}"
        data = source.get('data')
        if include_history and name != 'teachingProfile' and data is not None:
            if not data:
                text += '；无该项记录'
            else:
                brief = data[:3] if isinstance(data, list) else {
                    key: value[:2] if isinstance(value, list) else value for key, value in list(data.items())[:4]} if isinstance(data, dict) else data
                text += '；摘要=' + json.dumps(brief, ensure_ascii=False, separators=(',', ':'))[:800] + '；详细记录按需查询'
        lines.append(text)
    if not include_history:
        return '\n'.join(lines)
    if any(local['historical'].values()):
        lines.append('历史本地掌握/误区记录仅作历史观察，不是正式学情。')
    if local['observations']:
        lines.append('本地观察=' + json.dumps(local['observations'][-3:], ensure_ascii=False, separators=(',', ':')))
    if local['preferences']:
        lines.append('偏好=' + json.dumps(local['preferences'], ensure_ascii=False, separators=(',', ':')))
    if view['currentSessionEvidence']:
        lines.append('本题证据=' + json.dumps(view['currentSessionEvidence'], ensure_ascii=False, separators=(',', ':'))[:2500])
    if not local['profile'].get('grade') and not basics.get('currentBookId'):
        lines.append('年级和册次未确认；手头一步结束后再单独询问。')
    return '\n'.join(lines)
