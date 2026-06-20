"""juggle_cmd_add_node — CLI handler for `juggle add-node` (P5).

Backs the unified add-node verb (spec §6.1). create-thread and graph add-task
are thin shims that delegate to juggle_add_node.add_node internally.
"""
from __future__ import annotations

import json
import sys

from juggle_cli_common import get_db


def cmd_add_node(args) -> None:
    """`juggle add-node` handler — creates a unified node."""
    from juggle_add_node import AddNodeError, add_node

    db = get_db(getattr(args, "db_path", None), init=True)

    objective = getattr(args, "objective", None) or ""
    if objective == "-":
        objective = sys.stdin.read().strip()

    deps_raw = getattr(args, "deps", None)
    deps = [t.strip() for t in (deps_raw or "").split(",") if t.strip()]
    rb_raw = getattr(args, "required_by", None)
    required_by = [t.strip() for t in (rb_raw or "").split(",") if t.strip()]

    try:
        result = add_node(
            db,
            kind=getattr(args, "kind", "task"),
            title=args.title,
            objective=objective,
            project_id=getattr(args, "project", None),
            deps=deps,
            required_by=required_by,
            verify_cmd=getattr(args, "verify_cmd", None),
            parent_id=getattr(args, "parent", None),
        )
    except AddNodeError as e:
        if getattr(args, "json_out", False):
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"add-node REFUSED: {e}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "json_out", False):
        print(json.dumps({"ok": True, **result}))
        return

    print(f"Created node {result['node_id']!r} (kind={getattr(args, 'kind', 'task')}, state={result['state']})")
