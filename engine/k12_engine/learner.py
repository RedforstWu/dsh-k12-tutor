"""Learner profile (``k12-learner/1``): structured facts about one student, never a chat transcript.

The tutor changes it only through validated update ops. Free text (evidence, summaries) is screened for
personal identifiers, so the profile cannot become a store of a minor's contact details.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path

from . import subjects

SCHEMA = "k12-learner/1"
MC_STATUS = ("active", "resolved")
PREFERENCES = ("pace", "explanation", "encouragement", "language", "notes")
KEEP_EVIDENCE = 3
KEEP_OBSERVATIONS = 6
KEEP_SUMMARIES = 12
MAX_TEXT = 300
REVIEW_STEPS = (1, 3, 7, 14, 30)
QUALIFIED = re.compile(r"^(?P<subject>[a-z]+)/(?P<stage>[a-z]+):(?P<id>[a-z0-9]+(?:-[a-z0-9]+)*)$")
PII = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "身份证号"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱"),
    (re.compile(r"(住在|家住|住址|地址是|门牌|小区\S{0,6}号楼|微信号|QQ号|qq号)"), "住址或联系方式"),
]


class LearnerError(ValueError):
    pass


def today() -> dt.date:
    return dt.date.today()


def default(learner_id: str) -> dict:
    return {"schema": SCHEMA, "learner_id": learner_id, "nickname": None, "grade": None, "textbooks": {},
            "mastery": {}, "misconceptions": {}, "preferences": {}, "review": [], "summaries": [],
            "updated": None}


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


def mastery_state(observations: list[dict]) -> str:
    """Code decides the label from evidence. Mastered needs the last two answers correct and unaided;
    not mastered needs the last two wrong; everything else, including a single answer, is uncertain."""
    last = observations[-2:]
    if len(last) == 2 and all(o["correct"] and o["independent"] for o in last):
        return "mastered"
    if len(last) == 2 and not any(o["correct"] for o in last):
        return "not_mastered"
    return "uncertain"


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


def _evidence(entry: dict, text: str, session: str, now: str) -> None:
    entry.setdefault("evidence", []).append({"at": now, "session": session, "note": text})
    entry["evidence"] = entry["evidence"][-KEEP_EVIDENCE:]


def _schedule(profile: dict, kp: str, on: dt.date, success: bool) -> None:
    current = next((r for r in profile["review"] if r["kp"] == kp), None)
    step = 0 if current is None or not success else min(current["step"] + 1, len(REVIEW_STEPS) - 1)
    profile["review"] = [r for r in profile["review"] if r["kp"] != kp]
    profile["review"].append({"kp": kp, "step": step, "due": (on + dt.timedelta(days=REVIEW_STEPS[step])).isoformat()})
    profile["review"].sort(key=lambda r: r["due"])


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
        elif op == "observe":
            kp = check_id(u.get("kp"), "kp")
            if not isinstance(u.get("correct"), bool) or not isinstance(u.get("independent"), bool):
                raise LearnerError("observe needs correct (true/false) and independent (true/false: no hint, no reveal)")
            entry = out["mastery"].setdefault(kp, {"state": "uncertain", "observations": []})
            entry["observations"] = (entry["observations"] + [{"at": stamp, "session": session, "correct": u["correct"],
                                     "independent": u["independent"], "note": screen(u.get("evidence", ""), "evidence")}])[-KEEP_OBSERVATIONS:]
            previous, entry["state"] = entry["state"], mastery_state(entry["observations"])
            entry["updated"] = stamp
            if entry["state"] == "mastered":
                _schedule(out, kp, now.date(), success=previous == "mastered")
            elif entry["state"] == "not_mastered":
                out["review"] = [r for r in out["review"] if r["kp"] != kp]
            done.append(f"observe {kp}: {entry['state']}")
        elif op == "misconception":
            mc = check_id(u.get("id"), "misconception")
            if u.get("status") not in MC_STATUS:
                raise LearnerError(f"status must be one of {', '.join(MC_STATUS)}")
            entry = out["misconceptions"].setdefault(mc, {"status": "active", "seen": 0})
            entry["status"] = u["status"]
            entry["seen"] += u["status"] == "active"
            entry["updated"] = stamp
            _evidence(entry, screen(u.get("evidence", ""), "evidence"), session, stamp)
            done.append(f"misconception {mc}={u['status']}")
        elif op == "preference":
            if u.get("key") not in PREFERENCES:
                raise LearnerError(f"preference key must be one of {', '.join(PREFERENCES)}")
            out["preferences"][u["key"]] = screen(u.get("value", ""), "value")
            done.append(f"preference {u['key']}")
        elif op == "summary":
            out["summaries"].append({"at": stamp, "session": session, "text": screen(u.get("text", ""), "text")})
            out["summaries"] = out["summaries"][-KEEP_SUMMARIES:]
            done.append("summary")
        else:
            raise LearnerError(f"updates[{i}].op must be basics, observe, misconception, preference or summary")
    out["updated"] = stamp
    return out, done


def summary(profile: dict, on: dt.date | None = None) -> str:
    """The few lines the tutor sees at the start of a session."""
    on = on or today()
    lines = []
    basics = [f"{profile['grade']}年级" if profile.get("grade") else "年级未知"]
    if profile.get("textbooks"):
        basics.append("教材 " + "，".join(f"{s}:{e}" for s, e in profile["textbooks"].items()))
    if profile.get("nickname"):
        basics.insert(0, f"称呼 {profile['nickname']}")
    lines.append("；".join(basics))
    shaky = [k for k, v in profile["mastery"].items() if v["state"] != "mastered"]
    solid = [k for k, v in profile["mastery"].items() if v["state"] == "mastered"]
    if solid:
        lines.append("已掌握：" + "，".join(solid[-6:]))
    if shaky:
        lines.append("未掌握或不确定：" + "，".join(shaky[-6:]))
    active = [k for k, v in profile["misconceptions"].items() if v["status"] == "active"]
    if active:
        lines.append("仍活跃的误区：" + "，".join(active[-6:]))
    due = [r["kp"] for r in profile["review"] if r["due"] <= on.isoformat()]
    if due:
        lines.append("今天到期该复习：" + "，".join(due[:5]))
    if profile.get("preferences"):
        lines.append("偏好：" + "；".join(f"{k}={v}" for k, v in profile["preferences"].items()))
    if profile.get("summaries"):
        last = profile["summaries"][-1]
        lines.append(f"上次学习（{last['at'][:10]}）：{last['text']}")
    if profile.get("grade") is None:
        lines.append("档案是空的：等手头这一步告一段落，再单独问一句年级和教材版本，用 learner_profile 记下来；不要和讲题的问题放在同一条回复里。")
    return "\n".join(lines)
