"""Operation registry: one name per engine capability, each a pure function of JSON args."""
from __future__ import annotations

from typing import Callable

from . import activities, experience, learner, mathcheck, route, session, subjects, study, study_host, study_practice, whiteboard


class OpError(ValueError):
    pass


EXPECTED = (mathcheck.MathError, learner.LearnerError, route.RouteError, session.SessionError, subjects.PackError,
            activities.ActivityError, whiteboard.BoardError, experience.ExperienceError, study.StudyError, study_host.transport.TransportError)


def preferences(args: dict) -> dict:
    if args.get("action") == "read":
        return experience.read_preferences(args["cwd"])
    if args.get("action") == "save":
        return experience.save_preferences(args["cwd"], args["preferences"])
    raise OpError("unknown experience action")

OPS: dict[str, Callable[[dict], object]] = {
    "study_binding_context": study_host.binding_context,
    "study_connect": study_host.connect,
    "study_status": study_host.status,
    "study_practice": study_practice.run,
    "study_pending_reviews": study_practice.pending_reviews,
    "study_practice_open": study_host.open_task,
    "study_watch_list": study_host.watch_list,
    "study_watch_state": study_practice.watch_state,
    "study_watch_claim": study_practice.watch_claim,
    "study_notification_ids": study_host.notification_ids,
    "study_web_assessment": study_practice.web_assessment,
    "study_web_baseline": study_practice.web_baseline,
    "study_web_read": study_practice.web_read,
    "study_web_certify": study_practice.web_certify,
    "study_web_revoke": study_practice.web_revoke,
    "study_web_audit_list": study_practice.web_audit_list,
    "study_web_exposure": study_practice.web_exposure,
    "study_continuation": study_practice.continuation,
    "study_mark_continued": study_practice.mark_continued,
    "study_record": study.record,
    "study_assessment": study.assessment,
    "study_query": study.query,
    "study_capture": study.capture,
    "exercise_capture": lambda args: study.capture(args, local_only=True),
    "exercise_record": lambda args: study.record(args, local_only=True),
    "exercise_notification_ids": study_host.notification_ids,
    "study_enable": study.enable,
    "study_flush": lambda args: study.flush(args, limit=5),
    "ping": lambda args: {"pong": True},
    "experience": preferences,
    "experience_workspace_owner": lambda args: {"legacyBound": study_host.transport.load_binding(args["cwd"]) is not None},
    "whiteboard": whiteboard.run,
    "math": mathcheck.run,
    "turn_begin": session.turn_begin,
    "route_task": session.route_task,
    "math_check": session.math_check,
    "learner_profile": session.learner_profile,
    "learner_context_plan": learner.refresh_plan,
    "learner_context_dirty": learner.dirty,
    "learner_legacy_binding": learner.legacy_binding,
    "make_activity": session.make_activity,
    "turn_end": session.turn_end,
}


def dispatch(op: str | None, args: dict) -> object:
    if op not in OPS:
        raise OpError(f"unknown op {op!r}")
    try:
        if op in ("turn_begin", "route_task", "math_check", "learner_profile", "learner_context_dirty", "make_activity", "turn_end", "study_record", "exercise_record", "whiteboard", "study_assessment"):
            from .locking import workspace_lock
            session.Workspace(args.get("cwd"), args.get("session"))
            with workspace_lock(args.get("cwd")):
                return OPS[op](args)
        return OPS[op](args)
    except EXPECTED as exc:
        raise OpError(str(exc)) from exc
