"""Operation registry: one name per engine capability, each a pure function of JSON args."""
from __future__ import annotations

from typing import Callable

from . import learner, mathcheck, route, session, subjects


class OpError(ValueError):
    pass


EXPECTED = (mathcheck.MathError, learner.LearnerError, route.RouteError, session.SessionError, subjects.PackError)

OPS: dict[str, Callable[[dict], object]] = {
    "ping": lambda args: {"pong": True},
    "math": mathcheck.run,
    "turn_begin": session.turn_begin,
    "route_task": session.route_task,
    "math_check": session.math_check,
    "learner_profile": session.learner_profile,
    "turn_end": session.turn_end,
}


def dispatch(op: str | None, args: dict) -> object:
    if op not in OPS:
        raise OpError(f"unknown op {op!r}")
    try:
        return OPS[op](args)
    except EXPECTED as exc:
        raise OpError(str(exc)) from exc
