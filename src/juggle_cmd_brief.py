"""juggle_cmd_brief — the `juggle brief [ID] [--json]` command handler.

ONE command, arg switches scope (thin dispatch only — collection lives in
``juggle_brief_collect`` / ``juggle_brief_probe``, formatting in
``juggle_brief_render``, blocker-diagnosis in ``juggle_brief_diagnose``):

  no arg   → session snapshot (active topics only, agents tmux-reconciled)
  <topic>  → topic probe + computed why-stalled verdict   (e.g. SS)
  <P#>     → project graph rollup                          (e.g. P4)

``--json`` dumps the collected state dict verbatim (the deterministic contract an
agent asserts on); otherwise the matching ``render_*_human`` formatter prints it.
"""
from __future__ import annotations

import json as _json
import sys

from juggle_cli_common import get_db


def cmd_brief(args) -> None:
    db = get_db(getattr(args, "db_path", None))
    ident = getattr(args, "id", None)
    json_out = getattr(args, "json_out", False)

    if not ident:
        from juggle_brief_collect import collect_session_state
        from juggle_brief_render import render_session_human

        state = collect_session_state(db)
        _emit(state, json_out, render_session_human)
        return

    # An ID that resolves to a project (P#) is a project probe; otherwise a topic.
    if db.get_project(ident) is not None:
        from juggle_brief_probe import collect_project_state
        from juggle_brief_render import render_project_human

        state = collect_project_state(db, ident)
        _emit(state, json_out, render_project_human)
        return

    from juggle_brief_probe import collect_topic_state
    from juggle_brief_render import render_topic_human

    state = collect_topic_state(db, ident)
    if state is None:
        print(f"No topic or project matching '{ident}'.")
        sys.exit(1)
    _emit(state, json_out, render_topic_human)


def _emit(state: dict, json_out: bool, render) -> None:
    if json_out:
        print(_json.dumps(state, default=str))
    else:
        print(render(state))
