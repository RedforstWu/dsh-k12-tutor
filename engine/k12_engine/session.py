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
from pathlib import Path

from . import activities, experience, guard, learner, mathcheck, route

STATE_SCHEMA = "k12-session/1"
COMPACT_AFTER_TURNS = 6
ACTIVITY_NUDGES = 2  # reminders per task while the recommended activity page is not made
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
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"schema": STATE_SCHEMA, "session": self.session, "turn": 0, "tasks": [], "flags": [],
                "turns_since_profile": 0, "steered": [], "started": False}

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
    return state["tasks"][-1] if state["tasks"] else None


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
    if not state["started"]:
        state["started"] = True
        state["break_mark"] = now.isoformat(timespec="seconds")
        notes.append("[学情] " + learner.summary(ws.profile()))
    elif now - dt.datetime.fromisoformat(state.get("break_mark") or now.isoformat()) >= BREAK_AFTER:
        state["break_mark"] = now.isoformat(timespec="seconds")
        notes.append("[时长] 这次已经连续学习半小时左右：回复末尾用一句话提醒学生起来活动、看看远处，休息一下再继续。")
    if (now.hour >= BEDTIME_HOUR or now.hour < 5) and not state.get("bedtime_noted"):
        state["bedtime_noted"] = True
        notes.append(f"[作息] 现在是晚上 {now:%H:%M}：这一轮回复末尾温和地提醒学生早点休息；作业确实做不完，可以如实告诉老师。")
    task = current_task(state)
    state["last_text"] = text[:500]
    if task is not None and text:
        task["student_texts"].append(text[:500])
        task["student_turns"] += 1
    notes += [f["note"] for f in flags]
    if any(f["kind"] in ("crisis", "harm") for f in flags):
        ws.append("alerts.jsonl", {"kind": [f["kind"] for f in flags if f["kind"] in ("crisis", "harm")],
                                   "match": [f["match"] for f in flags], "for": "家长或老师",
                                   "action": "请尽快与孩子当面沟通；如有紧急危险请拨打 110 或 120"})
    if task is None:
        notes.append("[路由] 这段学习任务还没有路由：先判断学生要做什么，调用 route_task(action=start)，再回复。")
    else:
        notes.append(f"[路由] 当前任务 {task['id']}：playbook={task['playbook']}，知识点={','.join(task['knowledge_points']) or '无'}，"
                     f"答案策略={task['answer_policy']}。学生换了题目或目标时，重新 route_task(action=start)。")
    if activity_due(state, task) and not any(f["kind"] in ("crisis", "harm") for f in flags):
        task["activity_nudges"] = task.get("activity_nudges", 0) + 1
        notes.append(f"[互动] 这段任务推荐的互动网页还没做：{task['activity']['what']}。学生需要自己看到变化或矛盾时，"
                     "用 make_activity 做出来，再 present 发给学生。")
    if state["turns_since_profile"] >= COMPACT_AFTER_TURNS:
        notes.append("[学情] 已经多轮没有更新学情档案：这一轮结束前，把能确认的掌握度、误区和一句学习小结写进 learner_profile。")
    ws.save(state)
    return {"notes": notes, "flags": state["flags"]}


def activity_due(state: dict, task: dict | None) -> bool:
    """The task has a recommended page, the student has replied at least once, and no page was made for it yet."""
    return (task is not None and bool(task.get("activity")) and task["student_turns"] >= 1
            and task.get("activity_nudges", 0) < ACTIVITY_NUDGES
            and not any(a["task"] == task["id"] for a in state.get("activities", [])))


def route_task(args: dict) -> dict:
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    action = args.get("action", "start")
    if action == "start":
        from .whiteboard import invalidate
        invalidate(ws)
        state.pop('board_selection', None)
        result = route.start(args, ws.profile())
        previous = current_task(state)
        # The message that opened the task counts as context, not as an attempt.
        task = {"id": f"t{len(state['tasks']) + 1}", **result["task"], "answers": [],
                "student_texts": [state["last_text"]] if state.get("last_text") else [],
                "student_turns": 0, "revealed": False}
        task["problem_answers"] = solve_problem(task.get("problem") or "")
        if task["answer_policy"] != "refuse":
            task["activity"] = activities.suggest([k.split(":")[-1] for k in task["knowledge_points"]], task["playbook"])
        task["experience"] = experience.task_context(experience.read_preferences(ws.root.parent),
                                                      task["knowledge_points"], task["answer_policy"])
        state["tasks"].append(task)
        ws.save(state)
        text = route.render(result)
        if task["experience"].get("context"):
            text += "\n\n[兴趣情境] " + task["experience"]["context"] + task["experience"]["instruction"]
        if previous is not None and state["turns_since_profile"] > 1:
            text += "\n\n- 上一个任务还没写学情：找合适的时机用 learner_profile 记下掌握度和误区。"
        return {"task_id": task["id"], "answer_policy": task["answer_policy"],
                "knowledge_points": task["knowledge_points"], "playbook": task["playbook"],
                "experience": task["experience"], "text": text}
    if action == "reveal":
        task = current_task(state)
        if task is None:
            raise SessionError("no open task: call route_task(action=start) first")
        ok, why = route.can_reveal(task, task["student_turns"])
        if ok:
            task["answer_policy"] = "revealed"
            task["revealed"] = True
            task["reveal_reason"] = str(args.get("reason") or "")[:200]
            ws.save(state)
        return {"allowed": ok, "why": why, "answer_policy": task["answer_policy"]}
    raise SessionError("action must be start or reveal")


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
    task = current_task(state)
    if task is not None and value.get("answers") and counts_as_answer(args, task):
        task["answers"] = sorted(set(task["answers"]) | set(value["answers"]))
        if task.get("problem") and not task.get("problem_answers"):  # a practice set has no problem to protect
            task["problem_answers"] = value["answers"]
        ws.save(state)
        if task["answer_policy"] in ("withhold", "refuse"):
            value["policy"] = "这是给你核对用的结果。答案策略是 " + task["answer_policy"] + "：不要把最终结果直接告诉学生。"
    return value


def learner_profile(args: dict) -> dict:
    ws = Workspace(args.get("cwd"), args.get("session"))
    action = args.get("action", "read")
    profile = ws.profile()
    if action == "read":
        return {"profile": profile, "summary": learner.summary(profile)}
    if action != "update":
        raise SessionError("action must be read or update")
    from . import study
    if study.enabled(str(ws.root.parent)) and any(update.get("op") == "observe" for update in (args.get("updates") or [])):
        raise SessionError("integrated workspace records answers with study_record; observe is disabled")
    updated, done = learner.apply(profile, args.get("updates"), ws.session)
    learner.save(ws.learner_path, updated)
    state = ws.state()
    state["turns_since_profile"] = 0
    ws.save(state)
    return {"applied": done, "summary": learner.summary(updated)}


def make_activity(args: dict) -> dict:
    """Build an activity page for the open task and write it into the student's workspace."""
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    task = current_task(state)
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
    made.append({"activityId": activity_id, "paramsHash": params_hash, "task": task["id"] if task else None, "kind": args.get("kind"), "template": page.template,
                 "file": f"activities/{name}", "title": page.title})
    ws.save(state)
    return {"activityId": activity_id, "paramsHash": params_hash, "path": str(path), "title": page.title, "kind": args.get("kind"), "template": page.template,
            "next": f"用 present 把它发给学生：files=[{{path: \"{path.as_posix()}\", description: \"{page.title}\"}}]。"
                    "然后请学生点开对话里的卡片（在右侧打开），做完回来说说结果。"}


def turn_end(args: dict) -> dict:
    """The tutor finished a reply: return one steering message, or none."""
    ws = Workspace(args.get("cwd"), args.get("session"))
    state = ws.state()
    task = current_task(state) or {}
    reply = args.get("reply") or ""
    violations = guard.inspect_reply(reply, {
        "answer_policy": task.get("answer_policy"), "answers": task.get("answers", []),
        "student_texts": task.get("student_texts", []), "flags": state.get("flags", [])})
    key = f"{args.get('turn')}"
    fresh = [v for v in violations if f"{key}:{v['kind']}" not in state["steered"]]
    for v in fresh:
        state["steered"].append(f"{key}:{v['kind']}")
        ws.append("guard.jsonl", {"turn": args.get("turn"), "task": task.get("id"), "kind": v["kind"],
                                  **({"answers": v["answers"]} if "answers" in v else {})})
    ws.save(state)
    return {"steer": "\n".join(v["steer"] for v in fresh) or None, "violations": [v["kind"] for v in violations]}
