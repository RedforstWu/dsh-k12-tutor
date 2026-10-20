"""``python -m k12_engine serve`` runs the worker; ``python -m k12_engine <op> '<json args>'`` runs one op."""
from __future__ import annotations

import json
import sys

from . import ops
from .server import serve


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print("ops:", ", ".join(sorted(ops.OPS)))
        return 0
    if argv[0] == "serve":
        serve()
        return 0
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    try:
        print(json.dumps(ops.dispatch(argv[0], args), ensure_ascii=False, indent=2))
    except ops.OpError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
