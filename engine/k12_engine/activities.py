"""Interactive activity pages: built from a vetted template, from an interaction-scheme component, or written
on the spot by the tutor, and delivered to the student as an HTML file the DSH Web UI opens in its sidebar.

Every page is one self-contained HTML file in ``<workspace>/activities/``. The engine checks the math of
template parameters with sympy (a balance only takes a linear equation, every step of an explainer must be
equivalent to the one before), applies the task's answer policy, and pins what a page may load with a
Content-Security-Policy: template and custom pages load nothing from the network; a scheme page may only
frame its ai-view component.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

import sympy as sp

from . import experience, guard, mathcheck

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "activities" / "templates"
SCHEMES_DIR = ROOT / "activities" / "schemes"
AI_VIEW = "https://ai-view.new.ndhy.com"
MAX_CUSTOM_BYTES = 80_000
CSP_LOCAL = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:"
CSP_SCHEME = f"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-src {AI_VIEW}"
FORBIDDEN = [  # custom pages: no network, no navigation away, no nested pages, no forms
    (re.compile(r"<\s*(iframe|frame|object|embed|form|base)\b", re.I), "不能嵌入其他页面或表单"),
    (re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts)\b"), "不能联网"),
    (re.compile(r"\b(window\.open|location\s*=|location\.(href|assign|replace)|top\.|parent\.)"), "不能跳转或访问外层页面"),
    (re.compile(r"""(src|href|action)\s*=\s*["']?\s*(https?:|//)""", re.I), "不能加载外部资源"),
    (re.compile(r"@import|url\(\s*['\"]?\s*(https?:|//)", re.I), "不能加载外部样式"),
]


class ActivityError(ValueError):
    pass


@dataclass(frozen=True)
class Page:
    """A prepared page: what the template needs, the answers its math has, and whether it shows a full
    worked solution (then it may not be about the withheld problem itself)."""
    template: str
    params: dict
    title: str
    answers: tuple[str, ...] = ()
    shows_solution: bool = False


# ── math helpers ──────────────────────────────────────────────────────────────

def _relation(text: str) -> sp.Basic:
    try:
        parsed = mathcheck.parse(text)
    except mathcheck.MathError as exc:
        raise ActivityError(str(exc)) from exc
    if not parsed.is_relation:
        raise ActivityError(f"需要一个方程或不等式：{text}")
    return parsed.value


def _one_var(expr: sp.Basic, what: str) -> sp.Symbol:
    symbols = sorted(expr.free_symbols, key=lambda s: s.name)
    if len(symbols) != 1:
        raise ActivityError(f"{what}只能有一个未知数")
    return symbols[0]


def _rational(value) -> list[int]:
    q = sp.nsimplify(value)
    if not q.is_Rational:
        raise ActivityError(f"系数要是有理数：{value}")
    return [int(q.p), int(q.q)]


def _linear_side(side: sp.Expr, x: sp.Symbol) -> tuple[sp.Expr, sp.Expr]:
    poly = sp.Poly(sp.expand(side), x)
    if poly.degree() > 1:
        raise ActivityError("这里只接受一次式")
    return poly.coeff_monomial(x), poly.coeff_monomial(1)


def _answers(texts: list[str], variables: list[str] | None = None) -> tuple[str, ...]:
    return tuple(mathcheck.solve(texts, variables).get("answers", []))


# ── templates ─────────────────────────────────────────────────────────────────

TERM = re.compile(r"([+-]?)\s*(\(\s*[+-]?\s*\d+\s*\)|\d+)")


def number_line(p: dict) -> Page:
    expr = mathcheck.normalize(str(p.get("expression", "")))
    if not re.fullmatch(r"[\d\s+\-()]+", expr):
        raise ActivityError("expression 只能是整数的加减，例如 -6+(-8)")
    terms = [(sign, int(re.sub(r"[()\s]", "", body))) for sign, body in TERM.findall(expr)]
    if len(terms) < 2 or len(terms) > 5:
        raise ActivityError("expression 要有 2 到 5 个数")
    start = terms[0][1] * (-1 if terms[0][0] == "-" else 1)
    moves = [n * (-1 if sign == "-" else 1) for sign, n in terms[1:]]
    result = start + sum(moves)
    if sp.sympify(expr.replace(" ", "")) != result:
        raise ActivityError(f"没能把 {expr} 拆成数轴上的移动")
    path = [start] + [start + sum(moves[:i + 1]) for i in range(len(moves))]
    lo, hi = min(path + [0]) - 2, max(path + [0]) + 2
    if hi - lo < 20:
        pad = (20 - (hi - lo)) // 2
        lo, hi = lo - pad, hi + pad
    if hi - lo > 60:
        raise ActivityError("数太大，数轴放不下（范围请控制在 ±30 以内）")
    title = p.get("title") or "数轴上走一走"
    question = p.get("question") or f"算一算 {pretty(expr)}：从 {start} 出发，在数轴上按每一项走一步。"
    return Page("number-line", {"title": title, "question": question, "start": start, "moves": moves,
                                "result": result, "range": [lo, hi]}, title, (str(result),))


def balance(p: dict) -> Page:
    eq = _relation(str(p.get("equation", "")))
    if not isinstance(eq, sp.Eq):
        raise ActivityError("天平只放方程（用 =）")
    x = _one_var(eq, "方程")
    (a, b), (c, d) = _linear_side(eq.lhs, x), _linear_side(eq.rhs, x)
    if a == c:
        raise ActivityError("这个方程没有唯一解，换一个")
    title = p.get("title") or "等式天平：两边做同样的事"
    return Page("balance", {"title": title, "variable": x.name, "left": {"x": _rational(a), "c": _rational(b)},
                            "right": {"x": _rational(c), "c": _rational(d)}}, title,
                _answers([str(p["equation"])]))


def resource_comparison(p: dict) -> Page:
    title = p.get("title") or "两站资源何时相同"
    return Page("resource-comparison", {"title": title, "initial": [20, 5], "rates": [3, 6],
                                       "maxTime": 10}, title, _answers(["20+3t=5+6t"]))


SLIDER_K = [k / 2 for k in range(-6, 7)]
SLIDER_B = list(range(-4, 5))


def kb_sliders(p: dict) -> Page:
    tasks = p.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 4:
        raise ActivityError("tasks 要有 1 到 4 个任务")
    clean = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict) or not str(t.get("text", "")).strip():
            raise ActivityError(f"tasks[{i}] 需要 text")
        task = {"text": str(t["text"]), "k_sign": t.get("k_sign"), "k": t.get("k"), "b": t.get("b"), "through": t.get("through")}
        if task["k_sign"] not in (None, "pos", "neg"):
            raise ActivityError("k_sign 只能是 pos 或 neg")
        if task["k"] is not None and task["k"] not in SLIDER_K:
            raise ActivityError("k 要在 -3 到 3 之间，步长 0.5")
        if task["b"] is not None and task["b"] not in SLIDER_B:
            raise ActivityError("b 要是 -4 到 4 的整数")
        doable = [(k, b) for k in SLIDER_K for b in SLIDER_B if k != 0
                  and (task["k_sign"] != "pos" or k > 0) and (task["k_sign"] != "neg" or k < 0)
                  and (task["k"] is None or k == task["k"]) and (task["b"] is None or b == task["b"])
                  and (task["through"] is None or abs(k * task["through"][0] + b - task["through"][1]) < 1e-9)]
        if not doable:
            raise ActivityError(f"tasks[{i}] 用滑块做不到（k 在 -3 到 3、步长 0.5，b 是 -4 到 4 的整数）")
        clean.append(task)
    title = p.get("title") or "拖动滑块，看看 k 和 b 怎样改变直线"
    return Page("kb-sliders", {"title": title, "tasks": clean}, title)


def zero_product(p: dict) -> Page:
    eq = _relation(str(p.get("equation", "")))
    if not isinstance(eq, sp.Eq):
        raise ActivityError("需要一个方程")
    x = _one_var(eq, "方程")
    poly = sp.Poly(sp.expand(eq.lhs - eq.rhs), x)
    if poly.degree() != 2:
        raise ActivityError("这里放一元二次方程")
    roots = sp.solve(poly.as_expr(), x)
    if not roots or not all(r.is_Rational for r in roots):
        raise ActivityError("请用有理数根、能因式分解的方程，便于学生检验")
    a, b, c = (float(poly.coeff_monomial(x ** k)) for k in (2, 1, 0))
    title = p.get("title") or "找全方程的根"
    return Page("zero-product", {"title": title, "equation": pretty(str(p["equation"])), "variable": x.name,
                                 "coefficients": [a, b, c], "roots": [float(r) for r in roots]}, title,
                _answers([str(p["equation"])]))


def inequality_line(p: dict) -> Page:
    rel = _relation(str(p.get("inequality", "")))
    ops = {sp.Lt: "<", sp.Le: "<=", sp.Gt: ">", sp.Ge: ">="}
    op = next((v for k, v in ops.items() if isinstance(rel, k)), None)
    if op is None:
        raise ActivityError("需要一个一元一次不等式（<、>、≤、≥）")
    x = _one_var(rel, "不等式")
    (a, b), (c, d) = _linear_side(rel.lhs, x), _linear_side(rel.rhs, x)
    if a == c:
        raise ActivityError("这个不等式化简后没有未知数")
    edge = (d - b) / (a - c)
    flips = (a - c) < 0
    right = (op in (">", ">=")) != flips
    title = p.get("title") or "在数轴上画出解集"
    e = float(edge)
    return Page("inequality-line", {"title": title, "inequality": pretty(str(p["inequality"])), "variable": x.name,
                                    "sides": [float(a), float(b), float(c), float(d)], "op": op, "edge": e,
                                    "direction": "right" if right else "left", "inclusive": op in ("<=", ">="),
                                    "range": [int(e) - 6, int(e) + 6]}, title,
                _answers([str(p["inequality"])]))


def substitution_table(p: dict) -> Page:
    given, find = str(p.get("given", "")), str(p.get("find", ""))
    if not (re.fullmatch(r"[A-Za-z]", given) and re.fullmatch(r"[A-Za-z]", find)) or given == find:
        raise ActivityError("given 和 find 是两个不同的字母")

    def as_linear(text: str) -> dict:
        eq = _relation(text)
        sol = sp.solve(eq, sp.Symbol(find))
        if len(sol) != 1:
            raise ActivityError(f"{text} 解不出唯一的 {find}")
        m, n = _linear_side(sol[0], sp.Symbol(given))
        return {"m": float(m), "n": float(n)}

    values = p.get("values") or [1, 2, 10]
    if not all(isinstance(v, (int, float)) for v in values) or not 2 <= len(values) <= 6:
        raise ActivityError("values 是 2 到 6 个数")
    student, correct = as_linear(str(p["student_equation"])), as_linear(str(p["correct_equation"]))
    if student == correct:
        raise ActivityError("student_equation 和 correct_equation 是同一个关系，表格看不出问题。"
                            "correct_equation 要写按题意正确的式子，先用 math_check 代一组数核对")
    title = p.get("title") or "代个数检查你的式子"
    return Page("substitution-table", {
        "title": title, "story": str(p.get("story", "")), "student_equation": pretty(str(p["student_equation"])),
        "given": given, "find": find, "given_label": str(p.get("given_label", given)),
        "find_label": str(p.get("find_label", find)), "relation_hint": str(p.get("relation_hint", p.get("story", ""))),
        "values": values, "student": student, "correct": correct},
        title)


SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def pretty(text: str) -> str:
    """How a student writes the math the engine parsed: x², 3x, ≤, a real minus sign."""
    text = re.sub(r"\^\(?(\d+)\)?", lambda m: m.group(1).translate(SUPERSCRIPT), text)
    text = re.sub(r"(\d)\s*\*\s*(?=[A-Za-z(])", r"\1", text)
    return text.replace("<=", "≤").replace(">=", "≥").replace("*", "×").replace("-", "−")


def _equivalent(a: str, b: str) -> bool:
    pa, pb = mathcheck.parse(a), mathcheck.parse(b)
    if pa.is_relation and pb.is_relation:
        return mathcheck.equivalent(a, b)["equivalent"]
    if not pa.is_relation and not pb.is_relation:
        return sp.simplify(pa.value - pb.value) == 0
    return False


def stepwise(p: dict) -> Page:
    steps = p.get("steps")
    if not isinstance(steps, list) or not 2 <= len(steps) <= 12:
        raise ActivityError("steps 要有 2 到 12 步")
    clean = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not str(s.get("say", "")).strip():
            raise ActivityError(f"steps[{i}] 需要 say（这一步说什么）")
        clean.append({"say": str(s["say"]), "show": str(s["show"]) if s.get("show") else ""})
    if p.get("check", True):
        shown = [(i, re.sub(r"\[\[|\]\]", "", s["show"])) for i, s in enumerate(clean) if s["show"]]
        math_steps = []
        for i, text in shown:
            try:
                mathcheck.parse(text)
                math_steps.append((i, text))
            except mathcheck.MathError:
                continue  # a step that shows words, not math, is not checked
        for (i, a), (j, b) in zip(math_steps, math_steps[1:]):
            if not _equivalent(a, b):
                raise ActivityError(f"第 {i + 1} 步 {a} 到第 {j + 1} 步 {b} 不等价，请改正后再生成（或对非推导内容设 check=false）")
    title = p.get("title") or "分步讲解"
    ending = p.get("ending") or "看完了。回到对话里，用自己的话说说关键的那一步。"
    texts = " ".join(f"{s['say']} {s['show']}" for s in clean)
    shown = [{"say": s["say"], "show": pretty(s["show"])} for s in clean]
    return Page("stepwise", {"title": title, "steps": shown, "ending": ending}, title, shows_solution=True,
                answers=(texts,))


TEMPLATES: dict[str, dict] = {
    "resource-comparison": {"build": resource_comparison, "kps": ["equation-concept"], "name": "两站资源比较",
                            "use": "先预测，再调节时间观察两站资源，自己写等量关系；回到对话验算并做无主题迁移题",
                            "example": {}},
    "number-line": {"build": number_line, "kps": ["rational-add-sub", "rational-number-line"], "name": "数轴上走一走",
                    "use": "有理数加减：学生先猜结果，再看在数轴上一步步走", "example": {"expression": "-6+(-8)"}},
    "balance": {"build": balance, "kps": ["linear-eq-solve", "equation-concept"], "name": "等式天平",
                "use": "一元一次方程：学生自己选择两边同时做的操作，体会移项来自等式性质", "example": {"equation": "3x+20=4x-25"}},
    "kb-sliders": {"build": kb_sliders, "kps": ["linear-function-graph", "linear-function-concept"], "name": "k、b 滑块",
                   "use": "一次函数：拖动 k、b 完成任务，看图象怎样变化",
                   "example": {"tasks": [{"text": "让直线从左往右下降，并经过点 (0, 3)", "k_sign": "neg", "b": 3}]}},
    "zero-product": {"build": zero_product, "kps": ["quadratic-eq-solve"], "name": "找全方程的根",
                     "use": "一元二次方程：学生填根、代回检验，图象显示是否找全（针对两边除以含未知数的式子丢根）",
                     "example": {"equation": "x(x-2)+x-2=0"}},
    "inequality-line": {"build": inequality_line, "kps": ["linear-inequality-solve", "inequality-properties"],
                        "name": "不等式数轴", "use": "一元一次不等式：学生代数试验、在数轴上画解集（针对乘除负数不变号）",
                        "example": {"inequality": "-2x+3>7"}},
    "substitution-table": {"build": substitution_table, "kps": ["algebraic-expression-write"], "name": "代入检验表",
                           "use": "列代数式：用具体数检验学生写的式子是否合理（针对字母当对象的反转错误）",
                           "example": {"story": "某校学生人数是老师人数的 6 倍", "student_equation": "6S=T",
                                       "correct_equation": "S=6T", "given": "T", "find": "S",
                                       "given_label": "老师人数", "find_label": "学生人数"}},
    "stepwise": {"build": stepwise, "kps": [], "name": "分步讲解动画",
                 "use": "任何知识点：逐步展示推导，每步一句讲解；相邻两步的式子由引擎验算等价。展示完整过程，只用于变式题或已揭晓的题",
                 "example": {"title": "移项为什么变号", "steps": [
                     {"say": "原方程。", "show": "3x+20=4x-25"},
                     {"say": "两边同时减去 [[4x]]。", "show": "3x-4x+20=-25"},
                     {"say": "两边同时减去 [[20]]。", "show": "3x-4x=-25-20"}]}},
}


@lru_cache(maxsize=None)
def schemes() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCHEMES_DIR.glob("*.json"))}


def _validate_schema(instance, schema: dict) -> None:
    import jsonschema

    try:
        jsonschema.validate(instance, schema, cls=jsonschema.Draft202012Validator)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(x) for x in exc.absolute_path) or "参数"
        raise ActivityError(f"方案参数不符合要求（{where}）：{exc.message[:200]}") from exc


def scheme(p: dict) -> Page:
    scheme_id = p.get("scheme")
    table = schemes()
    if scheme_id not in table:
        raise ActivityError(f"scheme 只能是 {', '.join(table)}")
    spec = table[scheme_id]
    params = p.get("params")
    if not isinstance(params, dict):
        raise ActivityError("params 是一个对象，格式见方案说明")
    _validate_schema(params, spec["params"])
    style = p.get("style") or "cartoon"
    if style not in spec["components"]:
        raise ActivityError(f"style 只能是 {', '.join(spec['components'])}")
    question = str(params.get("question", ""))
    fallback = re.sub(r"【[^】]*】", "____", question)
    for key in ("options", "questions"):
        items = params.get(key)
        if isinstance(items, list):
            fallback += "\n" + "\n".join(str(i.get("text", i) if isinstance(i, dict) else i) for i in items)
    title = params.get("title") or spec["name"]
    texts = [json.dumps(params, ensure_ascii=False), *re.findall(r"【([^】]*)】", question)]
    options = params.get("options") if isinstance(params.get("options"), list) else []
    for key in (params.get("answer") if isinstance(params.get("answer"), list) else [params.get("answer")]):
        texts += [str(o) for o in options if isinstance(o, str) and isinstance(key, str) and o.strip().startswith(key)]
    return Page("scheme-host", {"component": spec["components"][style], "params": params, "outcomes": spec["outcomes"],
                                "hint": "做完以后回到对话里告诉老师结果。", "fallback": fallback.strip()}, str(title),
                answers=tuple(texts), shows_solution=True)


def custom(p: dict) -> Page:
    html = str(p.get("html", ""))
    if not html.strip():
        raise ActivityError("custom 需要 html")
    if len(html.encode("utf-8")) > MAX_CUSTOM_BYTES:
        raise ActivityError(f"页面太大（上限 {MAX_CUSTOM_BYTES // 1000} KB），精简一下")
    for pattern, why in FORBIDDEN:
        hit = pattern.search(html)
        if hit:
            raise ActivityError(f"页面{why}：{hit.group(0)}")
    title = str(p.get("title") or "互动练习")
    return Page("custom", {"html": html}, title, answers=(re.sub(r"<[^>]+>", " ", html),), shows_solution=True)


BUILDERS: dict[str, Callable[[dict], Page]] = {"scheme": scheme, "custom": custom}


# ── policy and rendering ──────────────────────────────────────────────────────

def check_policy(page: Page, task: dict | None) -> None:
    """The task's answer policy holds inside pages too."""
    if not task:
        return
    policy, context = task.get("answer_policy"), task.get("assessment_context")
    if policy == "refuse":
        raise ActivityError("这个任务不做互动页（考试中或越界请求）")
    if policy != "withhold":
        return
    # Only the answers of the task's own problem are protected: exercises and variants the tutor solved for a page
    # are fair to show. A variant may share a root with the problem, not its whole solution set.
    answers = task.get("problem_answers") or []
    if not answers:
        return
    if page.shows_solution:
        exact = {guard.normalize(a) for a in answers}
        leaked = guard.stated_answers(" ".join(page.answers), answers) + [a for a in page.answers if guard.normalize(a) in exact]
        if leaked:
            raise ActivityError(f"互动页里出现了这道题的答案（{'，'.join(leaked)}）。换一道变式题（换数字或结构）再做。")
    elif context == "homework" and set(answers) <= set(page.answers):
        raise ActivityError("这是作业原题：互动页做完会显示结果。换一道同类的变式题来做，原题留给学生自己完成。")


@lru_cache(maxsize=None)
def _base_css() -> str:
    return (TEMPLATES_DIR / "_base.css").read_text(encoding="utf-8")


def _json_for_script(value) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def render(page: Page, theme_id: str = "classic", preferences: dict | None = None) -> str:
    css = _base_css() + experience.presentation_css(theme_id, preferences)
    theme_marker = f'<meta name="k12-theme" content="{theme_id}">'
    if page.template == "custom":
        html = page.params["html"]
        head = (f'<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP_LOCAL}">'
                f"{theme_marker}<style>{css}</style>")
        if re.search(r"<head[^>]*>", html, re.I):
            return re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + head, html, count=1, flags=re.I)
        return f'<!doctype html><html lang="zh-CN"><head>{head}<title>{page.title}</title></head><body>{html}</body></html>'
    source = (TEMPLATES_DIR / f"{page.template}.html").read_text(encoding="utf-8")
    if page.template == "resource-comparison":
        source = source.replace("__RESOURCE_LABEL__", experience.theme(theme_id)["resourceLabel"])
    csp = CSP_SCHEME if page.template == "scheme-host" else CSP_LOCAL
    source = source.replace("<head>", f'<head><meta http-equiv="Content-Security-Policy" content="{csp}">{theme_marker}', 1)
    return source.replace("/*__BASE_CSS__*/", css).replace("/*__PARAMS__*/null", _json_for_script(page.params))


def build(kind: str, args: dict) -> Page:
    if kind == "template":
        name = args.get("template")
        if name not in TEMPLATES:
            raise ActivityError(f"template 只能是 {', '.join(TEMPLATES)}")
        params = dict(args.get("params") or {})
        if args.get("title"):
            params.setdefault("title", args["title"])
        try:
            return TEMPLATES[name]["build"](params)
        except (ActivityError, mathcheck.MathError):
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            example = json.dumps(TEMPLATES[name]["example"], ensure_ascii=False)
            raise ActivityError(f"{name} 的 params 缺项或格式不对（{type(exc).__name__}: {exc}）。照这个格式填：{example}") from exc
    if kind in BUILDERS:
        return BUILDERS[kind](args)
    raise ActivityError("kind 只能是 template、scheme 或 custom")


def slug(text: str) -> str:
    ascii_part = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return ascii_part[:24] or "page"
