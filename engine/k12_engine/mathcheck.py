"""Exact math for the tutor: parse school notation, solve, simplify, and check a student's work with sympy.

The tutor computes the right result here before it explains anything, and the plugin keeps every
solved answer so the reply-side guard can tell when a reply hands the answer over.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass

import sympy as sp
from sympy.parsing.sympy_parser import (
    convert_xor,
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
RELATIONS = (("<=", sp.Le), (">=", sp.Ge), ("≤", sp.Le), ("≥", sp.Ge), ("<", sp.Lt), (">", sp.Gt), ("=", sp.Eq))
SCHOOL = str.maketrans({"×": "*", "÷": "/", "（": "(", "）": ")", "，": ",", "－": "-", "−": "-", "﹣": "-", "–": "-", "≤": "<=", "≥": ">=", "＜": "<", "＞": ">", "＋": "+", "＝": "=",
                        "²": "^2", "³": "^3", "√": "sqrt", "π": "pi", "　": " ", "：": ":", "；": ";"})
# Every single letter is a variable in school algebra, including S, N, E, I and Q, which sympy would otherwise
# read as its own singletons and number sets.
LETTERS = {c: sp.Symbol(c) for c in string.ascii_letters}
ACTIONS = ("solve", "simplify", "factor", "expand", "evaluate", "equivalent", "check_solution")


class MathError(ValueError):
    pass


@dataclass(frozen=True)
class Parsed:
    text: str
    value: sp.Basic  # an expression, or a relation (Eq / Lt / ...)

    @property
    def is_relation(self) -> bool:
        return isinstance(self.value, sp.core.relational.Relational)


def normalize(text: str) -> str:
    text = text.translate(SCHOOL).strip()
    text = re.sub(r"sqrt\s*(\d+(?:\.\d+)?|[a-zA-Z])", r"sqrt(\1)", text)
    return text.replace("==", "=")


def _expr(text: str) -> sp.Expr:
    try:
        return parse_expr(text, local_dict=dict(LETTERS), transformations=TRANSFORMS, evaluate=True)
    except Exception as exc:
        raise MathError(f"cannot parse {text!r}: {type(exc).__name__}") from exc


def parse(text: str) -> Parsed:
    clean = normalize(text)
    if re.search(r"[^\x00-\x7f]", clean):
        raise MathError(f"cannot parse {text!r}: write only the math, without words")
    for symbol, relation in RELATIONS:
        if symbol in clean:
            left, _, right = clean.partition(symbol)
            if symbol == "=" and (left.endswith(("<", ">")) or right.startswith("=")):
                continue
            return Parsed(text, relation(_expr(left), _expr(right), evaluate=False))
    return Parsed(text, _expr(clean))


def _symbols(items: list[Parsed], names: list[str] | None) -> list[sp.Symbol]:
    if names:
        return [sp.Symbol(n) for n in names]
    found = set().union(*(p.value.free_symbols for p in items))
    return sorted(found, key=lambda s: s.name)


def show(value) -> str:
    return sp.sstr(value).replace("**", "^").replace("sqrt", "√")


def interval_text(solution_set: sp.Set, x: sp.Symbol) -> str:
    """School notation for a solution set of one variable: ``x < -2``, ``-1 ≤ x < 3``, ``x < 1 或 x > 2``."""
    if solution_set == sp.S.EmptySet:
        return "无解"
    if solution_set == sp.S.Reals:
        return f"{x} 为任意实数"
    if isinstance(solution_set, sp.Union):
        return " 或 ".join(interval_text(part, x) for part in solution_set.args)
    if isinstance(solution_set, sp.FiniteSet):
        return " 或 ".join(f"{x}={show(v)}" for v in solution_set)
    if not isinstance(solution_set, sp.Interval):
        return show(solution_set)
    lo, hi = solution_set.start, solution_set.end
    left = "" if lo == -sp.oo else f"{show(lo)} {'<' if solution_set.left_open else '≤'} "
    right = "" if hi == sp.oo else f" {'<' if solution_set.right_open else '≤'} {show(hi)}"
    if not left and right:
        return f"{x}{right}"
    if left and not right:
        return f"{x} {'>' if solution_set.left_open else '≥'} {show(lo)}"
    return f"{left}{x}{right}"


def solve(texts: list[str], variables: list[str] | None = None) -> dict:
    items = [parse(t) for t in texts]
    syms = _symbols(items, variables)
    if not syms:
        raise MathError("nothing to solve for: no variable")
    if any(isinstance(p.value, (sp.Lt, sp.Le, sp.Gt, sp.Ge)) for p in items):
        if len(syms) != 1:
            raise MathError("inequalities: one variable only")
        solution = sp.reduce_inequalities([p.value for p in items], syms[0])
        text = interval_text(solution.as_set(), syms[0])
        return {"kind": "inequality", "solution": text, "answers": [text]}
    equations = [p.value if p.is_relation else sp.Eq(p.value, 0) for p in items]
    solutions = sp.solve(equations, syms, dict=True)
    # School algebra works over the reals; parameters (like T in S=6T) may stay symbolic.
    real = [s for s in solutions if all(v.is_real is not False for v in s.values())]
    if not real:
        none = "无实数解" if solutions else "无解"
        return {"kind": "equation", "solutions": [], "answers": [none]}
    rows = [{show(k): show(v) for k, v in s.items()} for s in real]
    answers = sorted({f"{k}={v}" for row in rows for k, v in row.items()} | {v for row in rows for v in row.values()})
    return {"kind": "equation", "solutions": rows, "answers": answers}


def transform(action: str, text: str) -> dict:
    p = parse(text)
    if p.is_relation:
        raise MathError(f"{action} takes an expression, not an equation")
    fn = {"simplify": sp.simplify, "factor": sp.factor, "expand": sp.expand}[action]
    result = fn(p.value)
    return {"result": show(result), "answers": [show(result)]}


def evaluate(text: str, values: dict[str, str] | None = None) -> dict:
    p = parse(text)
    subs = {sp.Symbol(k): _expr(normalize(str(v))) for k, v in (values or {}).items()}
    value = p.value.subs(subs)
    if p.is_relation:
        truth = sp.simplify(value)
        return {"result": str(bool(truth)) if truth in (sp.true, sp.false) else show(truth)}
    exact = sp.nsimplify(sp.simplify(value))
    out = {"result": show(exact), "answers": [show(exact)]}
    if exact.is_number and not exact.is_Integer:
        out["decimal"] = show(sp.N(exact, 6))
    return out


def equivalent(a: str, b: str, variables: list[str] | None = None) -> dict:
    """Same expression, or (for equations / inequalities) the same solution set."""
    pa, pb = parse(a), parse(b)
    if pa.is_relation != pb.is_relation:
        return {"equivalent": False, "reason": "one side is an equation, the other an expression"}
    if not pa.is_relation:
        same = sp.simplify(pa.value - pb.value) == 0
        return {"equivalent": bool(same)}
    sa, sb = solve([a], variables), solve([b], variables)
    key = "solution" if sa["kind"] == "inequality" else "solutions"
    if sa["kind"] != sb["kind"]:
        return {"equivalent": False, "reason": "different relation kinds"}
    if key == "solutions":
        same = sorted(map(str, sa[key])) == sorted(map(str, sb[key]))
    else:
        same = sa[key] == sb[key]
    return {"equivalent": bool(same), "left": sa.get(key), "right": sb.get(key)}


def check_solution(texts: list[str], answer: dict[str, str]) -> dict:
    """Substitute a student's answer into each equation; report which ones hold."""
    subs = {sp.Symbol(k): _expr(normalize(str(v))) for k, v in answer.items()}
    rows = []
    for text in texts:
        p = parse(text)
        rel = p.value if p.is_relation else sp.Eq(p.value, 0)
        holds = sp.simplify(rel.subs(subs))
        rows.append({"equation": text, "holds": bool(holds == sp.true)})
    return {"correct": all(r["holds"] for r in rows), "checks": rows}


def run(args: dict) -> dict:
    action = args.get("action")
    if action not in ACTIONS:
        raise MathError(f"action must be one of {', '.join(ACTIONS)}")
    exprs = args.get("exprs") or ([args["expr"]] if args.get("expr") else [])
    if not exprs or not all(isinstance(e, str) and e.strip() for e in exprs):
        raise MathError("give expr or exprs as non-empty strings")
    variables = args.get("variables") or None
    if action == "solve":
        return solve(exprs, variables)
    if action in ("simplify", "factor", "expand"):
        return transform(action, exprs[0])
    if action == "evaluate":
        return evaluate(exprs[0], args.get("values"))
    if action == "equivalent":
        if len(exprs) != 2:
            raise MathError("equivalent needs exactly two exprs")
        return equivalent(exprs[0], exprs[1], variables)
    if not isinstance(args.get("answer"), dict) or not args["answer"]:
        raise MathError('check_solution needs answer, e.g. {"x": "3"}')
    return check_solution(exprs, args["answer"])
