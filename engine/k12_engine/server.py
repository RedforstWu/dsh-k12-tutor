"""JSON-lines worker for the DSH plugin: one request per line in, one response per line out.

Request ``{"id": 1, "op": "math", "args": {...}}``; response ``{"id": 1, "ok": true, "value": ...}`` or
``{"id": 1, "ok": false, "error": "..."}``. The plugin keeps one worker per DSH process, so sympy is
imported once instead of on every tool call.
"""
from __future__ import annotations

import json
import sys
import traceback

from . import ops


def handle(line: str) -> dict:
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"id": None, "ok": False, "error": f"bad request: {exc}"}
    rid = request.get("id")
    try:
        return {"id": rid, "ok": True, "value": ops.dispatch(request.get("op"), request.get("args") or {})}
    except ops.OpError as exc:
        return {"id": rid, "ok": False, "error": str(exc)}
    except Exception as exc:  # the worker must outlive one bad request
        traceback.print_exc(file=sys.stderr)
        return {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def serve() -> None:
    for line in sys.stdin:
        if line.strip():
            sys.stdout.write(json.dumps(handle(line), ensure_ascii=False) + "\n")
            sys.stdout.flush()
