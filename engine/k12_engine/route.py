"""Routing one learning task: which playbook, which subject pack and knowledge points, and the answer policy.

The tutor supplies facts (what the student wants, whether it is homework or a live exam, the topic);
this module derives everything else from tables, so the answer policy is data, not the model's mood.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from . import subjects

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = ROOT / "skills" / "tutor-mode" / "references" / "playbooks"
METHODS = ROOT / "skills" / "tutor-mode" / "references" / "methods"
PRINCIPLES = ROOT / "skills" / "tutor-mode" / "references" / "principles"
CONTEXTS = ("none", "homework", "exam-prep", "exam-live", "unknown")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)

# Policy per (playbook kind, assessment context). withhold: no final answer until the student has worked;
# refuse: do not work the item at all; open: nothing to withhold.
POLICY = {
    "exam-live": "refuse",
    "boundary": "refuse",
    "problem": "withhold",
    "open": "open",
}
PROBLEM_PLAYBOOKS = {"solve", "diagnose", "practice", "writing", "figure-it-out"}
OPEN_PLAYBOOKS = {"emotion", "review-plan", "inquiry"}
MATH_ITEM = re.compile(r"[=<>≤≥]|\d\s*[-+×÷*/^]\s*[\d(a-z]|[a-z]\s*[-+*/^]\s*\d")


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class GuidanceCard:
    id: str
    name: str
    when: str
    principles: tuple[str, ...]
    status: str
    body: str


@lru_cache(maxsize=None)
def _cards(directory: Path) -> dict[str, GuidanceCard]:
    found = {}
    for path in sorted(directory.glob("*.md")):
        match = FRONTMATTER.match(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        if not match:
            raise RouteError(f"{path}: missing frontmatter")
        meta = yaml.safe_load(match.group(1)) or {}
        if meta.get("id") != path.stem:
            raise RouteError(f"{path}: id must equal the file name")
        found[path.stem] = GuidanceCard(path.stem, str(meta["name"]), str(meta["when"]),
                                        tuple(meta.get("principles") or ()), str(meta.get("status", "full")),
                                        match.group(2).strip())
    return found


def playbooks(directory: Path = PLAYBOOKS) -> dict[str, GuidanceCard]:
    return _cards(directory)


def methods(directory: Path = METHODS) -> dict[str, GuidanceCard]:
    return _cards(directory)


def validate_method(method: str | None) -> None:
    if method is not None and (not isinstance(method, str) or method not in methods()):
        raise RouteError(f"method must be omitted or one of {', '.join(methods())}")


def policy_for(playbook: str, context: str, has_problem: bool) -> str:
    if context == "exam-live":
        return POLICY["exam-live"]
    if playbook == "boundary":
        return POLICY["boundary"]
    if has_problem or playbook in PROBLEM_PLAYBOOKS:
        return POLICY["problem"]
    return POLICY["open"]


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _score(kp: subjects.KnowledgePoint, text: str) -> int:
    """Exact name or alias hits weigh most; bigram overlap with the name breaks ties between neighbours."""
    text = re.sub(r"\s+", "", text)  # "k 的意义" and "k的意义" are the same words
    score = sum(3 + len(term) for term in (kp.name, *kp.aliases) if term and re.sub(r"\s+", "", term) in text)
    return score + len(_bigrams(kp.name) & _bigrams(text))


def shape_hint(problem: str) -> str | None:
    """The knowledge point a parseable school-math problem belongs to by its form alone."""
    from . import mathcheck
    import sympy as sp

    parts = [p for p in re.split(r"[,，;；]", problem) if re.search(r"[=<>≤≥]", p)]
    relations = []
    for part in parts:  # pieces with words in them are the student's work or commentary; skip what does not parse
        try:
            parsed = mathcheck.parse(re.sub(r"^[^0-9a-zA-Z(（\-]+", "", part))
        except mathcheck.MathError:
            continue
        if parsed.is_relation:
            relations.append(parsed.value)
    if not relations:
        return None
    if any(isinstance(r, (sp.Lt, sp.Le, sp.Gt, sp.Ge)) for r in relations):
        return "linear-inequality-solve" if len(relations) == 1 else "inequality-system"
    exprs = [sp.together(r.lhs - r.rhs) for r in relations]
    symbols = set().union(*(e.free_symbols for e in exprs))
    if not symbols:
        return None
    if any(sp.denom(e).free_symbols for e in exprs):
        return "fractional-equation"
    if len(relations) >= 2 and len(symbols) >= 2:
        return "linear-system-solve" if len(symbols) == 2 else "three-var-system"
    exprs = exprs[:1]  # one unknown: the first relation is the problem, the rest is the student's work
    degree = sp.Poly(sp.numer(exprs[0]), *symbols).total_degree() if len(symbols) == 1 else None
    return {1: "linear-eq-solve", 2: "quadratic-eq-solve"}.get(degree)


def find_kps(pack: subjects.Pack, text: str, grade: int | None, limit: int = 3, problem: str = "") -> list[str]:
    hint = shape_hint(problem) if problem else None
    scored = [(_score(kp, text) + (1 if grade == kp.grade else 0) + (20 if kp.id == hint else 0), kp.id)
              for kp in pack.kps.values()]
    return [kp_id for s, kp_id in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 3][:limit]


def kp_card(pack: subjects.Pack, kp_id: str, edition: str | None) -> str:
    kp = pack.kps[kp_id]
    found_in, places = pack.locate(kp_id, edition)
    lines = [f"### {kp.name}（{pack.key}:{kp.id}）", "本地教学资料（非共享图谱）；以下章节、课标、基础和讲解内容均来自 Tutor 本地学科包。"]
    if places:
        label = pack.textbook_meta.get(found_in, {}).get("short", found_in)
        lines.append(f"教材位置（{label}）：" + "；".join(f"{c.grade}年级{c.term}册 第{c.no}章 {c.title} · {s.no} {s.title}"
                                                   for c, s in places))
        if edition and found_in != edition:
            lines.append(f"注意：学生用的 {edition} 里没找到这个知识点的位置，上面是 {found_in} 的位置。")
    else:
        lines.append("教材位置：未核实，不要向学生报章节号。")
    if kp.curriculum:
        lines.append("课标：" + "；".join(kp.curriculum))
    if kp.prerequisites:
        lines.append("本地讲解建议的基础（未做共享映射）：" + "，".join(f"{pack.kps[p].name}（{p}）" for p in kp.prerequisites if p in pack.kps))
    for summary in kp.explanation_summary:
        lines.append(f"- 讲解关键：{summary}")
    for mc_id in kp.misconceptions:
        mc = pack.misconceptions[mc_id]
        tag = "（证据弱，只作观察，不单独据此下结论）" if mc.level >= 5 else ""
        lines.append(f"- 误区 {pack.key}:{mc.id}「{mc.name}」{tag} 表现：{mc.symptom} 诊断：{mc.diagnostic} 纠正：{mc.remedy}")
    if kp.path:
        lines.append(f"讲解要点、例题、变式和分级提示：read {kp.path.as_posix()}")
    else:
        lines.append("这个知识点只有目录信息，没有例题库：自己出题前先用 math_check 算对。")
    return "\n".join(lines)


def candidate_packs(subject: str, stage: str | None) -> list[subjects.Pack]:
    if stage:
        return [subjects.pack(subject, stage)]
    found = []
    for entry in subjects.index()["subjects"]:
        if entry["id"] == subject:
            found += [subjects.pack(subject, st["id"]) for st in entry.get("stages") or [] if st.get("status") == "active"]
    return found


def start(args: dict, profile: dict) -> dict:
    """Validate a new task and render what the tutor needs for it."""
    books = playbooks()
    playbook = args.get("playbook")
    if playbook not in books:
        raise RouteError(f"playbook must be one of {', '.join(books)}")
    method = args.get('method')
    validate_method(method)
    context = args.get("assessment_context", "unknown")
    if context not in CONTEXTS:
        raise RouteError(f"assessment_context must be one of {', '.join(CONTEXTS)}")
    grade = args.get("grade") or profile.get("grade")
    subject = args.get("subject")
    problem = (args.get("problem") or "").strip()
    topic = (args.get("topic") or "").strip()
    stage = subjects.stage_of(grade) if grade else None
    record = {"playbook": playbook, "subject": subject, "stage": stage, "grade": grade, "assessment_context": context,
              "problem": problem or None, "topic": topic or None, "knowledge_points": [], "pack": None}
    notes = []
    pack, ids = None, []
    given = [str(k).split(":")[-1] for k in args.get("knowledge_points") or []]
    if subject and not stage:
        notes.append("不知道学生年级：先按题目所在的学段讲，等这一步告一段落再单独问年级，记进学情档案。")
    if subject:
        try:
            packs = candidate_packs(subject, stage)
        except subjects.PackError as exc:
            packs = []
            notes.append(f"没有可用的学科包（{exc}）：按 playbook 和通用原则辅导，不编造教材内容。")
        if not packs and not any("学科包" in n for n in notes):
            notes.append(f"{subject} 还没有学科包：按 playbook 和通用原则辅导，不编造教材内容。")
        best = []
        for candidate in packs:
            hits = [k for k in given if k in candidate.kps] or find_kps(candidate, f"{topic} {problem}", grade, problem=problem)
            if hits and (not best or len(hits) > len(best[1])):
                best = [candidate, hits]
        if best:
            pack, ids = best
        elif len(packs) == 1:
            pack = packs[0]
    edition = profile.get("textbooks", {}).get(subject) if subject else None
    if pack:
        record["pack"] = pack.key
        record["stage"] = record["stage"] or pack.stage
        unknown = [k for k in given if k not in pack.kps]
        if unknown:
            notes.append("这些知识点 id 不存在，已改为按题目和话题检索：" + "，".join(unknown))
        record["knowledge_points"] = [f"{pack.key}:{k}" for k in ids]
        if not ids:
            notes.append("学科包里没检索到对应知识点：先确认题目属于哪一章，再按通用原则辅导。")
        if not edition:
            notes.append("档案里没有教材版本：先按人教版（2024 年起的修订版）讲，问到时记下来。")
            edition = next(iter(pack.textbooks), None)
        record["textbook"] = edition
    record["answer_policy"] = policy_for(playbook, context, bool(MATH_ITEM.search(problem.lower())))
    return {"task": record, "guidance": {"playbook": playbook, **({'method': method} if method else {})},
            "notes": notes, "kp_ids": [k.split(":")[-1] for k in record["knowledge_points"]],
            "edition": edition}


def render(result: dict) -> str:
    task = result["task"]
    policy_text = {
        "withhold": "答案策略 withhold：不给本题最终答案和完整过程。确有教学需要时，引用本题已记录的真实尝试和理由申请 reveal；作业原题仍不揭晓。闲聊轮数、自述和技术失败不构成尝试。",
        "refuse": "答案策略 refuse：正在考试或属于越界请求，不讲这道题本身，按 playbook 处理。",
        "open": "答案策略 open：没有需要扣住的答案，照常讲解。",
    }[task["answer_policy"]]
    parts = [policy_text, "", guidance_text(result['guidance'])]
    if task.get("pack"):
        subject, stage = task["pack"].split("/")
        pack = subjects.pack(subject, stage)
        cards = [kp_card(pack, kp_id, result.get("edition")) for kp_id in result["kp_ids"]]
        if cards:
            parts += ["", "## 知识点", "已连接学习站时，用 study_data 查询共享图谱定义和关系；本地学科包只作讲解内容与离线参考，不自动映射知识点 ID。", *cards]
    if task['answer_policy'] != 'refuse':
        parts += ["", "学生需要操作、观察或比较时才选媒介；对话或现有白板能完成就继续。"
                  f"GenUI 可用且无需引擎校验时，可在正文用 dsh-ui 围栏；需要受控页面时 read {(PRINCIPLES.parent / 'activities.md').as_posix()}，make_activity 写明 purpose 后用 present 发送。"]
        parts += ["", (PRINCIPLES.parent / 'speech.md').read_text(encoding='utf-8').strip()]
    if result["notes"]:
        parts += ["", "## 注意", *[f"- {n}" for n in result["notes"]]]
    return "\n".join(parts)


def can_reveal(task: dict, has_attempt: bool) -> tuple[bool, str]:
    if task.get("answer_policy") == "refuse":
        return False, "这个任务不能揭晓答案（考试中或越界请求）"
    if task.get("assessment_context") in (None, "unknown"):
        return False, "题目情境尚未确认，不能揭晓最终答案；可继续用提示帮助学生尝试"
    if task.get("answer_policy") == "open":
        return True, "这个任务本来就没有扣住的答案"
    if task.get("assessment_context") == "homework":
        # 双减与教育部学生使用指南：作业原题不给可直接抄写的答案。帮助的上限是同构变式题的完整示范。
        return False, ("作业原题不揭晓最终答案。改为讲一道同构变式题（换数字或换结构）的完整过程，"
                       "再请学生回到原题自己做；原题只点出他卡住的那一步")
    if has_attempt is not True:
        return False, "需要引用这道题已记录的真实尝试并说明教学理由；闲聊轮数不构成尝试"
    return True, "已有本题尝试与教学理由，可以对照完整过程"


def guidance_text(guidance: dict) -> str:
    book = playbooks()[guidance['playbook']]
    parts = [f"## Playbook：{book.name}（{book.id}）"]
    if book.status != 'full':
        parts.append(f"资料状态 {book.status}：仅提供当前骨架范围，不代表完整学科教学覆盖。")
    parts.append(book.body)
    method = methods()[guidance['method']] if guidance.get('method') else None
    if method:
        parts += ["", f"## 方法：{method.name}（{method.id}）", method.body]
    if guidance.get('focus'):
        parts.append('当前着力点：' + guidance['focus'])
    if guidance.get('nextObservation'):
        parts.append('下一步观察：' + guidance['nextObservation'])
    principles = dict.fromkeys((*book.principles, *(method.principles if method else ())))
    if principles:
        parts.append("需要原则细节时 read：" + "，".join(
            f"{p} → {(PRINCIPLES / (p + '.md')).as_posix()}" for p in principles))
    return '\n\n'.join(parts)
