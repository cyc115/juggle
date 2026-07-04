"""juggle_cmd_graph_learnings — `juggle graph learnings` READ command
(graph-node learnings primitive, spec 2026-07-03 §3 + Final revisions §8).

Token-light read over the ``nodes.learnings`` column (Migration 70): one
``<node-id>: <text>`` line per non-empty node in completion order, then a
``N nodes, M with learnings`` footer. Three mutually-exclusive scopes — positional
node-id / ``--topic`` / ``--project``. ``--json`` emits
``{"items":[{"node_id","text","completed_at"}], "nodes":N, "with_learnings":M}``.

Exit codes (Final revisions §8): a node-id scope over a missing node → nonzero
(fail-loud); an empty topic/project scope → footer only, exit 0. Pure read, zero
mutation. get_db is resolved through juggle_cmd_graph so test monkeypatches
(cg.get_db) keep working.
"""
from __future__ import annotations

import json
import sys

from dbops import db_graph


def cmd_graph_learnings(args) -> None:
    """`juggle graph learnings <node-id> | --topic T | --project P [--json]`."""
    import juggle_cmd_graph as cg  # lazy: shared get_db patch surface

    db = cg.get_db(getattr(args, "db_path", None), init=False)
    node_id = getattr(args, "node_id", None)
    topic = getattr(args, "topic", None)
    project = getattr(args, "project", None)

    if node_id is not None:
        result = db_graph.read_learnings(db, node_id=node_id)
        if result["nodes"] == 0:
            print(f"Error: node {node_id!r} not found.", file=sys.stderr)
            sys.exit(1)
    elif topic is not None:
        result = db_graph.read_learnings(db, topic_id=topic)
    else:
        result = db_graph.read_learnings(db, project_id=project)

    if getattr(args, "json_out", False):
        print(json.dumps(result))
        return
    for it in result["items"]:
        print(f"{it['node_id']}: {it['text']}")
    print(f"{result['nodes']} nodes, {result['with_learnings']} with learnings")
