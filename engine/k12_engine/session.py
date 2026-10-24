"""Per-session tutoring state and the hooks the DSH plugin calls around every turn.

State lives in ``<workspace>/.k12/sessions/<session>.json`` next to the learner profile
(``<workspace>/.k12/learner.json``): one workspace is one student. The plugin stays a thin adapter;
everything that decides lives here and is testable without DSH.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from . import activities, experience, guard, learner, mathcheck, route

STATE_SCHEMA = "k12-session/2"
COMPACT_AFTER_TURNS = 6
BREAK_AFTER = dt.timedelta(minutes=30)
BEDTIME_HOUR = 21
SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class SessionError(ValueError):
    pass


class Workspace:
    def __init__(self, cwd: str, session: str):
        if not cwd or not Path(cwd).is_absolute():
            raise SessionError("cwd must be an absolute path")
        if not SESSION_ID.match(session or ""):
            raise SessionError("bad session id")
        self.root = Path(cwd) / ".k12"
        self.session = session
        self.learner_path = self.root / "learner.json"
        self.state_path = self.root / "sessions" / f"{session}.json"

    def profile(self) -> dict:
        return learner.load(self.learner_path, learner_id=self.root.parent.name)

    def state(self) -> dict:
        if self.state_path.is_file():
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if state.get('schema') == 'k12-session/1':
                raise SessionError('legacy session requires stopped writers and tools/workspace_state.py migrate before starting this engine')
            if state.get('schema') != STATE_SCHEMA or state.get('session') != self.session:
                raise SessionError('unsupported session schema or session identity; use the supported workspace upgrade/restore flow')
            return state
        return {"schema": STATE_SCHEMA, "session": self.session, "turn": 0, "tasks": [], "flags": [],
                "turns_since_profile": 0, "steered": [], "started": False, 'activeTaskRef': None}

    def save(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_bytes(json.dumps(state, ensure_ascii=False, indent=1).encode("utf-8"))
        tmp.replace(self.state_path)

    def append(self, name: str, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with (self.root / name).open("ab") as f:
            f.write((json.dumps({"at": stamp, "session": self.session, **record}, ensure_ascii=False) + "\n").encode("utf-8"))


def current_task(state: dict) -> dict | None:
    return next((task for task in state['tasks'] if task['taskRef'] == state.get('activeTaskRef')
                 and task.get('status') == 'active'), None)


def migrate_state(ws, state):
    from . import exercise, study
    backup = ws.state_path.with_suffix('.json.v1.bak')
    if not backup.exists():
        backup.write_bytes(ws.state_path.read_bytes())
    ledger_path = ws.root / 'study-sync.sqlite'
    ledger_backup = ws.root / 'study-sync.sqlite.v1.bak'
    if ledger_path.exists() and not ledger_backup.exists():
        with closing(sqlite3.connect(ledger_path)) as source, closing(sqlite3.connect(ledger_backup)) as destination:
            source.backup(destination)
    board_tasks = set()
    for path in (ws.root / 'boards').glob('*.json'):
        board = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(board, dict) and board.get('sessionId') == ws.session and 'exerciseId' not in board:
            board_tasks.add(board.get('taskId'))
    with study.database({'cwd': str(ws.root.parent), 'session': ws.session}, allow_legacy=True) as (db, _):
        for index, task in enumerate(state['tasks']):
            legacy = dict(task)
            task['taskRef'] = f'{ws.session}:{task["id"]}'
            task['status'] = 'active' if index == len(state['tasks']) - 1 else 'paused'
            task['goal'] = task.get('goal') or task.get('topic') or task.get('problem') or '继续学习'
            task['guidance'] = {'playbook': task['playbook']}
            rows = db.execute('SELECT * FROM exercises WHERE session=? AND task=?', (ws.session, task['id'])).fetchall()
            matches = [row for row in rows if exercise.question_text(json.loads(row['snapshot'])['question']['stem'])
                       == exercise.question_text(legacy.get('problem') or '')]
            if len(matches) == 1:
                task['activeExerciseId'] = matches[0]['id']
                exercise.ensure(db, matches[0], legacy)
            elif not rows and (legacy.get('problem') or legacy.get('answer_policy') in ('withhold', 'refuse', 'revealed')
                               or task['id'] in board_tasks):
                task['activeExerciseId'] = exercise.context_exercise(db, ws, task['id'], legacy, source='legacy')
            elif rows:
                task['recoveryRequired'] = True
                task['recoveryExerciseIds'] = [row['id'] for row in rows]
                for row in rows:
                    restricted = ({key: legacy[key] for key in ('assessment_context','answer_policy') if key in legacy}
                                  if legacy.get('assessment_context') in ('homework','exam-live') or legacy.get('answer_policy') == 'refuse' else None)
                    exercise.ensure(db, row, legacy if row in matches else restricted)
            for key in ('playbook', 'problem', 'answers', 'problem_answers', 'student_texts', 'student_turns',
                        'revealed', 'reveal_reason', 'answer_policy', 'assessment_context'):
                task.pop(key, None)
            state.setdefault('legacyBoardExerciseRefs', {})[task['id']] = task.get('activeExerciseId')
    state.update(schema=STATE_SCHEMA, activeTaskRef=state['tasks'][-1]['taskRef'] if state['tasks'] else None)
    ws.save(state)
    return state


def protected_task(ws, state=None, task=None):
    from .exercise import task_view
    state = state or ws.state()
    selected = task if task is not None else current_task(state)
    if selected is None and state.get('activeTaskRef'):
        return {'id': 'unresolved', 'answer_policy': 'refuse', 'recovery_required': True, 'answers': [], 'student_texts': []}
    return task_view(ws, selected)


def turn_begin(args: dict) -> dict:
    """Student message arrived: return the notes to inject into this step."""
    ws = Workspace(args.get("cwd"), args.get("session"))
    from .whiteboard import invalidate
    invalidate(ws)
    state = ws.state()
    text = args.get("text") or ""
    state["turn"] += 1
    state["turns_since_profile"] += 1
    flags = guard.inspect_input(text)
    state["flags"] = [{"kind": f["kind"], "match": f["match"]} for f in flags]
    notes = []
    now = dt.datetime.fromisoformat(args["now"]) if args.get("now") else dt.datetime.now().astimezone()
    initial_context = not state['started']
    if not state["started"]:
        state["started"] = True
        state["break_mark"] = now.isoformat(timespec="seconds")
    elif now - dt.datetime.fromisoformat(state.get("break_mark") or now.isoformat()) >= BREAK_AFTER:
        state["break_mark"] = now.isoformat(timespec="seconds")
        notes.append("[时长] 这次已经连续学习半小时左右：回复末尾用一句话提醒学生起来活动、看看远处，休息一下再继续。")
    if (now.hour >= BEDTIME_HOUR or now.hour < 5) and not state.get("bedtime_noted"):
        state["bedtime_noted"] = True
        notes.append(f"[作息] 现在是晚上 {now:%H:%M}：这一轮回复末尾温和地提醒学生早点休息；作业确实做不完，可以如实告诉老师。")
    task = current_task(state)
    state["last_text"] = text[:500]
    if task is not None and text:
        task['student_turns'] = task.get('student_turns', 0) + 1
        if task.get('activeExerciseId'):
            from .exercise import note_input
            note_input(ws, task['activeExerciseId'], text, flags)
    learner_context = learner.context(ws, state, event='turn_begin')
    if initial_context or task:
        notes.insert(0, '[学情] ' + learner.summary(learner_context, include_history=False))
    notes += [f["note"] for f in flags]
    if any(f["kind"] in ("crisis", "harm") for f in flags):
        ws.append("alerts.jsonl", {"kind": [f["kind"] for f in flags if f["kind"] in ("crisis", "harm")],
                                   "match": [f["match"] for f in flags], "for": "家长或老师",
                                   "action": "请尽快与孩子当面沟通；如有紧急危险请拨打 110 或 120"})
    if task is None:
        notes.append("[路由] 当前还没有持续学习任务。需要多轮教学时调用 route_task(action=start)；普通定义或短澄清可以直接简短回答，受保护具体题目须先建立题目身份。")
    else:
        protected = protected_task(ws, state, task)
        notes.append(f"[路由] 当前任务 {task['id']}：playbook={task['guidance']['playbook']}，method={task['guidance'].get('method') or '通用动作'}，知识点={','.join(task['knowledge_points']) or '无'}，"
                     f"答案策略={protected['answer_policy']}，exerciseId={task.get('activeExerciseId') or '无'}。同题换方法用 adapt，同目标新题用 open，回到旧题用 resume，新目标用 start。")
        if protected.get('recovery_required'):
            notes.append('[恢复] 当前题目引用无法验证。先显式 resume 并选择原题，关联展示和答案操作保持拒绝。')
    if state["turns_since_profile"] >= COMPACT_AFTER_TURNS:
        notes.append("[学情] 已经多轮没有更新学情档案；有可确认的新观察时引用账本 evidenceRefs，或保存偏好和一句学习小结；作答与改判仍只写 study_record。")
    ws.save(state)
    return {"notes": notes, "flags": state["flags"], 'learnerContext': learner_context}


def route_task(args: dict) -> dict:
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    action = args.get("action", "start")
    allowed = {
        'start': {'goal','playbook','method','subject','grade','topic','problem','knowledge_points','assessment_context'},
        'adapt': {'taskRef','playbook','method','focus','nextObservation','reason','evidenceRefs'},
        'resume': {'taskRef','exerciseId'}, 'finish': {'taskRef','reason'},
        'reveal': {'taskRef','exerciseId','reason','evidenceRefs'},
    }
    if action not in allowed or set(args) - {'cwd','session','action'} - allowed[action]:
        raise SessionError('invalid task command fields')
    if action in ('adapt','resume','finish') and not args.get('taskRef'):
        raise SessionError('taskRef must reference this session task')
    if action == "start":
        from .whiteboard import invalidate
        invalidate(ws)
        state.pop('board_selection', None)
        if any(flag['kind'] == 'exam-live' for flag in state.get('flags', [])):
            args = {**args, 'assessment_context': 'exam-live'}
        result = route.start(args, ws.profile())
        previous = current_task(state)
        # The message that opened the task counts as context, not as an attempt.
        initial = result['task']
        task_id = f"t{len(state['tasks']) + 1}"
        task = {'id': task_id, 'taskRef': f'{ws.session}:{task_id}',
                **{key: value for key, value in initial.items() if key not in ('playbook', 'problem', 'answer_policy', 'assessment_context')},
                'goal': str(args.get('goal') or initial.get('topic') or initial.get('problem') or '继续学习')[:500],
                'status': 'active', 'student_turns': 0,
                'guidance': result['guidance']}
        task["experience"] = experience.task_context(experience.read_preferences(ws.root.parent),
                                                      task["knowledge_points"], initial["answer_policy"])
        if previous:
            previous['status'] = 'paused'
        if initial.get('problem') or initial['answer_policy'] == 'refuse' or initial['assessment_context'] == 'homework':
            task['recoveryRequired'] = True
        state["tasks"].append(task)
        state['activeTaskRef'] = task['taskRef']
        ws.save(state)
        if initial.get('problem') or initial['answer_policy'] == 'refuse' or initial['assessment_context'] == 'homework':
            from . import exercise, study
            with study.database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
                exercise_id = exercise.context_exercise(db, ws, task_id, {**initial, 'student_texts': [state['last_text']] if state.get('last_text') else []})
            task['activeExerciseId'] = exercise_id
            task.pop('recoveryRequired', None)
            protected = exercise.read(ws, exercise_id, task_id)
            initial.update(answer_policy=protected['answer_policy'], assessment_context=protected['assessment_context'])
            task['experience'] = experience.task_context(experience.read_preferences(ws.root.parent),
                                                         task['knowledge_points'], initial['answer_policy'])
            try:
                ws.save(state)
            except OSError:
                return {'state': 'navigation_failed', 'task_id': task_id, 'taskRef': task['taskRef'],
                        'exerciseId': exercise_id, 'answer_policy': initial['answer_policy'],
                        'recovery': 'resume this taskRef and exerciseId'}
        text = route.render(result)
        if task["experience"].get("context"):
            text += "\n\n[兴趣情境] " + task["experience"]["context"] + task["experience"]["instruction"]
        if previous is not None and state["turns_since_profile"] > 1:
            text += "\n\n- 有新的教学观察时用 learner_profile 引用账本证据；不要重复保存作答事实。"
        from .exercise import summary
        return {"task_id": task["id"], 'taskRef': task['taskRef'], 'exerciseId': task.get('activeExerciseId'), 'guidance': task['guidance'], "answer_policy": initial["answer_policy"],
                'exercise': summary(ws, task.get('activeExerciseId')),
                "knowledge_points": task["knowledge_points"], "playbook": task['guidance']['playbook'],
                "experience": task["experience"], "text": text}
    task = (next((item for item in state['tasks'] if args.get('taskRef') in (item['taskRef'], item['id'])), None)
            if args.get('taskRef') else current_task(state))
    if task is None:
        raise SessionError('taskRef must reference this session task')
    if action == 'reveal':
        from .exercise import reveal
        exercise_id = args.get('exerciseId') or task.get('activeExerciseId')
        if exercise_id != task.get('activeExerciseId'):
            raise SessionError('resume the exercise before revealing it')
        return reveal(ws, exercise_id, str(args.get('reason') or ''), args.get('evidenceRefs'))
    if action == 'adapt':
        if not args.get('reason'):
            raise SessionError('adapt requires a reason')
        if args.get('playbook') and args['playbook'] not in route.playbooks():
            raise SessionError('invalid playbook')
        route.validate_method(args.get('method'))
        if args.get('evidenceRefs'):
            from .study import database
            with database({'cwd': str(ws.root.parent), 'session': ws.session}) as (db, _):
                for reference in args['evidenceRefs']:
                    if not db.execute('SELECT 1 FROM evidence WHERE ref=? AND session=?', (reference, ws.session)).fetchone():
                        raise SessionError('adapt evidenceRefs must reference captured evidence in this session')
        guidance = dict(task['guidance'])
        for key in ('playbook', 'method', 'focus', 'nextObservation'):
            if key in args:
                if args[key] is None:
                    guidance.pop(key, None)
                else:
                    guidance[key] = args[key]
        task['guidance'] = guidance
        task['lastAdaptation'] = {'reason': str(args['reason'])[:500], 'evidenceRefs': args.get('evidenceRefs') or []}
    elif action == 'resume':
        from .exercise import read
        exercise_id = args.get('exerciseId') or task.get('activeExerciseId')
        if task.get('recoveryRequired') and not args.get('exerciseId'):
            raise SessionError('choose an original exerciseId to recover this task')
        if exercise_id and not read(ws, exercise_id, task['id']):
            raise SessionError('exercise must belong to this session task')
        previous = current_task(state)
        if previous and previous is not task:
            previous['status'] = 'paused'
        task.update(status='active')
        if exercise_id:
            task['activeExerciseId'] = exercise_id
        task.pop('recoveryRequired', None)
        state['activeTaskRef'] = task['taskRef']
        from .whiteboard import invalidate
        invalidate(ws)
        state.pop('board_selection', None)
    elif action == 'finish':
        if not args.get('reason'):
            raise SessionError('finish requires a reason')
        task.update(status='closed', closeSummary=str(args['reason'])[:1000])
        if state.get('activeTaskRef') == task['taskRef']:
            state['activeTaskRef'] = None
            from .whiteboard import invalidate
            invalidate(ws)
            state.pop('board_selection', None)
    else:
        raise SessionError('action must be start, adapt, resume, finish or reveal')
    ws.save(state)
    view = protected_task(ws, state, task)
    from .exercise import summary
    return {'task_id': task['id'], 'taskRef': task['taskRef'], 'exerciseId': task.get('activeExerciseId'),
            'status': task['status'], 'guidance': task['guidance'], 'answer_policy': view['answer_policy'],
            'exercise': summary(ws, task.get('activeExerciseId')),
            'knowledge_points': task['knowledge_points'], 'text': route.guidance_text(task['guidance'])}


def solve_problem(problem: str) -> list[str] | None:
    """Answers of the task's problem when route_task got it as plain math; else the first solve fills them in."""
    try:
        action = "solve" if mathcheck.parse(problem).is_relation else "evaluate"
        return mathcheck.run({"action": action, "expr": problem}).get("answers") or None
    except Exception:  # best effort: text, photos and anything sympy cannot take fall back to the first solve
        return None


def counts_as_answer(args: dict, task: dict) -> bool:
    """Solving is how the tutor gets an answer to withhold, for the task's problem or an exercise it set; a value
    it evaluates or simplifies is an answer only when that expression is part of the task's problem (an
    arithmetic item), not an illustration such as 6S for S=60."""
    if args.get("action") == "solve":
        return True
    if args.get("action") not in ("evaluate", "simplify", "factor") or args.get("values"):
        return False  # substituting values checks a candidate answer; it does not produce the task's answer
    expr = guard.normalize(str(args.get("expr") or ""))
    return bool(expr) and expr in guard.normalize(task.get("problem") or "")


def math_check(args: dict) -> dict:
    ws = Workspace(args.get("cwd"), args.get("session"))
    value = mathcheck.run(args)
    state = ws.state()
    task = protected_task(ws, state)
    if task is not None and value.get("answers") and counts_as_answer(args, task):
        if task.get('exerciseId'):
            from .exercise import record_math
            record_math(ws, task['exerciseId'], value['answers'])
        if task["answer_policy"] in ("withhold", "refuse"):
            value["policy"] = "这是给你核对用的结果。答案策略是 " + task["answer_policy"] + "：不要把最终结果直接告诉学生。"
    return value


def learner_profile(args: dict) -> dict:
    ws = Workspace(args.get("cwd"), args.get("session"))
    action = args.get("action", "read")
    allowed = {'cwd', 'session', 'action'} | ({'refresh'} if action == 'read' else {'updates'})
    if set(args) - allowed or ('refresh' in args and not isinstance(args['refresh'], bool)):
        raise SessionError('invalid learner profile fields')
    profile = ws.profile()
    if action == "read":
        view = learner.context(ws, refresh=args.get('refresh', False))
        return {'context': view, 'summary': learner.summary(view)}
    if action != "update":
        raise SessionError("action must be read or update")
    learner.validate_observations(ws, args.get('updates'))
    updated, done = learner.apply(profile, args.get("updates"), ws.session)
    learner.save(ws.learner_path, updated)
    state = ws.state()
    state["turns_since_profile"] = 0
    ws.save(state)
    view = learner.context(ws, state, action='update')
    return {'applied': done, 'context': view, 'summary': learner.summary(view)}


def make_activity(args: dict) -> dict:
    """Build an activity page for the open task and write it into the student's workspace."""
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    task = protected_task(ws, state)
    purpose = args.get('purpose')
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 500:
        raise SessionError('make_activity purpose must describe a student action in 1..500 characters')
    purpose = purpose.strip()
    page = activities.build(args.get("kind"), args)
    activities.check_policy(page, task)
    preferences = experience.read_preferences(ws.root.parent)
    theme_id = args.get("themeId", preferences["themeId"])
    experience.theme(theme_id)
    made = state.setdefault("activities", [])
    name = f"{task['id'] if task else 'free'}-{len(made) + 1:02d}-{page.template}.html"
    path = ws.root.parent / "activities" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(activities.render(page, theme_id, preferences).encode("utf-8"))
    activity_id = str(uuid.uuid4())
    params_hash = hashlib.sha256(json.dumps(args.get("params") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    association = {'taskRef': task['taskRef'] if task else None,
                   'exerciseId': task.get('activeExerciseId') if task else None, 'purpose': purpose}
    made.append({"activityId": activity_id, "paramsHash": params_hash, **association, "task": task["id"] if task else None, "kind": args.get("kind"), "template": page.template,
                 "file": f"activities/{name}", "title": page.title})
    ws.save(state)
    return {"activityId": activity_id, "paramsHash": params_hash, **association, "path": str(path), "title": page.title, "kind": args.get("kind"), "template": page.template,
            "next": f"用 present 把它发给学生：files=[{{path: \"{path.as_posix()}\", description: \"{page.title}\"}}]。"
                    "然后请学生点开对话里的卡片（在右侧打开），做完回来说说结果。"}


def turn_end(args: dict) -> dict:
    """The tutor finished a reply: return one steering message, or none."""
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    task = protected_task(ws, state) or {}
    reply = args.get("reply") or ""
    violations = guard.inspect_reply(reply, {
        "answer_policy": task.get("answer_policy"), "answers": sorted(set(task.get("answers", [])) | set(task.get('problem_answers') or [])),
        "student_texts": task.get("student_texts", []), "flags": state.get("flags", [])})
    key = f"{args.get('turn')}"
    fresh = [v for v in violations if f"{key}:{v['kind']}" not in state["steered"]]
    for v in fresh:
        state["steered"].append(f"{key}:{v['kind']}")
        ws.append("guard.jsonl", {"turn": args.get("turn"), "task": task.get("id"), "kind": v["kind"],
                                  **({"answers": v["answers"]} if "answers" in v else {})})
    ws.save(state)
    return {"steer": "\n".join(v["steer"] for v in fresh) or None, "violations": [v["kind"] for v in violations]}
