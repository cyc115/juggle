"""juggle_cmd_autopilot — `juggle autopilot` arm/disarm/on/off/status (Phase 4).

Owns: the arming surface. The settings-table key ``autopilot_armed_project``
is the SOLE arming authority (DA M6); ``~/.juggle/autopilot`` stays an
existence-only cache for the global toggle (hooks check existence only).
Arming while the global flag is already ON must ARM, never invert the flag
off (no rm-as-disarm flip logic).
Must not own: dispatching (juggle_graph_dispatch), graph load/validation
(juggle_cmd_graph), or hook injection (juggle_hooks_autopilot).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from juggle_cli_common import get_db
from juggle_autopilot_state import (
    ARMED_PROJECT_KEY,
    get_armed_projects,
    set_armed_projects,
)

AUTOPILOT_FLAG = Path.home() / ".juggle" / "autopilot"


def _flag_set(on: bool) -> None:
    if on:
        AUTOPILOT_FLAG.parent.mkdir(parents=True, exist_ok=True)
        AUTOPILOT_FLAG.touch(exist_ok=True)
    else:
        AUTOPILOT_FLAG.unlink(missing_ok=True)


def graphs_dir() -> Path:
    """Decomposition specs live at <data_dir>/graphs/<project>-graph.md (DA m3)."""
    from dbops.schema import DEFAULT_DATA_DIR

    return Path(DEFAULT_DATA_DIR) / "graphs"


def _cmd_arm_removed() -> None:
    """P7: per-project arming is removed — exit 1 with clear message."""
    print(
        "Error: `juggle autopilot arm/disarm` is removed (P7). "
        "The tick now dispatches all active projects automatically. "
        "Use `juggle autopilot on` / `juggle autopilot off` for the global toggle.",
        file=sys.stderr,
    )
    sys.exit(1)


def _cmd_off(db) -> None:
    """off: clear the global autopilot flag."""
    set_armed_projects(db, [])  # clear legacy key for clean state
    _flag_set(False)
    print(f"AUTOPILOT OFF.")


def _cmd_status(db, json_out: bool) -> None:
    from dbops.db_topics import topic_counts
    from juggle_graph_status import format_progress, graph_counts

    global_on = AUTOPILOT_FLAG.exists()
    armed = get_armed_projects(db)
    graphs = {}
    for pid in armed:
        tc, nc = topic_counts(db, pid), graph_counts(db, pid)
        graphs[pid] = {"topics": tc, "tasks": nc} if (tc or nc) else None
    diverged = bool(armed) and not global_on
    if json_out:
        first = armed[0] if armed else None
        print(json.dumps({
            "global_on": global_on,
            "armed_projects": armed,
            "graphs": graphs,
            "diverged": diverged,
            "armed_project": first,                         # deprecated (1 release)
            "graph": graphs.get(first) if first else None,  # deprecated
        }))
        return
    print(f"Autopilot global: {'ON' if global_on else 'OFF'}")
    if not armed:
        print("Armed projects: (none)")
    else:
        print(f"Armed projects ({len(armed)}): {', '.join(armed)}")
        for pid in armed:
            info = graphs[pid]
            if not info:
                print(f"  {pid}: no graph loaded")
                continue
            seg = []
            if info["topics"]:
                seg.append("topics " + format_progress(info["topics"]))
            if info["tasks"]:
                seg.append("tasks " + format_progress(info["tasks"]))
            print(f"  {pid}: " + "; ".join(seg))
    if diverged:
        print(
            "WARNING: settings key and flag file diverge — project(s) "
            f"{', '.join(armed)} armed but the global flag ({AUTOPILOT_FLAG}) "
            "is OFF: hooks inject nothing while the tick still dispatches."
        )


def cmd_autopilot(args) -> None:
    """Handler for `juggle autopilot <on|off|status>`."""
    cmd = args.autopilot_command
    if cmd in ("arm", "disarm"):
        _cmd_arm_removed()
        return
    db = get_db(getattr(args, "db_path", None), init=True)
    if cmd == "on":
        _flag_set(True)
        print("AUTOPILOT ON (global). The tick dispatches all active projects.")
    elif cmd == "off":
        _cmd_off(db)
    else:
        _cmd_status(db, getattr(args, "json_out", False))


def register(subparsers) -> None:
    """Register the `autopilot` subcommand tree on the main CLI parser."""
    p = subparsers.add_parser(
        "autopilot", help="Autopilot arming (settings authority) + global toggle"
    )
    sub = p.add_subparsers(dest="autopilot_command", required=True)
    p_arm = sub.add_parser("arm", help="Arm autopilot for a project (turns global ON)")
    p_arm.add_argument("project", help="Project id to arm")
    p_arm.set_defaults(func=cmd_autopilot)
    for name, hlp in (
        ("disarm", "Disarm a project from the armed set (global flag unchanged)"),
        ("on", "Global autopilot ON (flag cache only)"),
        ("off", "Disarm one or all projects + clear global flag when empty"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("project", nargs="?", default=None, help="Project id (optional)")
        sp.set_defaults(func=cmd_autopilot)
    p_st = sub.add_parser("status", help="Show global flag + armed projects + graphs")
    p_st.add_argument("--json", dest="json_out", action="store_true")
    p_st.set_defaults(func=cmd_autopilot, project=None)
