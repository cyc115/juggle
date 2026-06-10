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
from juggle_graph_dispatch import ARMED_PROJECT_KEY

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


def _cmd_arm(db, project_id: str) -> None:
    from juggle_graph_status import format_progress, graph_counts

    project = db.get_project(project_id)
    if not project:
        print(f"Error: project {project_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    db.set_setting(ARMED_PROJECT_KEY, project_id)
    _flag_set(True)  # arm while ON keeps ON — no rm-inversion (DA M6)
    spec = graphs_dir() / f"{project_id}-graph.md"
    try:
        graphs_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    counts = graph_counts(db, project_id)
    print(f"AUTOPILOT ON — project {project_id} ({project['name']}) armed.")
    if counts:
        print(f"Graph: {counts['total']} node(s) loaded — {format_progress(counts)}.")
        print(f"Spec: {spec}")
        print("The watchdog tick now claims and dispatches ready nodes.")
    else:
        print(
            "No graph loaded yet. Decompose the objective into a spec at "
            f"{spec}, get user approval (or --auto-approve), then run: "
            f"juggle project-graph load {spec} --project {project_id}"
        )


def _cmd_status(db, json_out: bool) -> None:
    from juggle_graph_status import format_progress, graph_counts

    global_on = AUTOPILOT_FLAG.exists()
    armed = (db.get_setting(ARMED_PROJECT_KEY) or "").strip() or None
    counts = graph_counts(db, armed) if armed else None
    if json_out:
        print(
            json.dumps(
                {
                    "global_on": global_on,
                    "armed_project": armed,
                    "graph": counts,
                }
            )
        )
        return
    print(f"Autopilot global: {'ON' if global_on else 'OFF'}")
    print(f"Armed project: {armed or '(none)'}")
    if armed and counts:
        print(f"Graph: {format_progress(counts)}")
    elif armed:
        print("Graph: no graph loaded")


def cmd_autopilot(args) -> None:
    """Handler for `juggle autopilot <arm|disarm|on|off|status>`."""
    db = get_db(getattr(args, "db_path", None), init=True)
    cmd = args.autopilot_command
    if cmd == "arm":
        _cmd_arm(db, args.project)
    elif cmd == "disarm":
        db.set_setting(ARMED_PROJECT_KEY, None)
        print(
            "Project disarmed (tick falls back to notify-only). "
            f"Global autopilot: {'ON' if AUTOPILOT_FLAG.exists() else 'OFF'}."
        )
    elif cmd == "on":
        _flag_set(True)
        print("AUTOPILOT ON (global). No project armed — use: juggle autopilot arm <project>")
    elif cmd == "off":
        db.set_setting(ARMED_PROJECT_KEY, None)
        _flag_set(False)
        print("AUTOPILOT OFF — project disarmed, global flag cleared. "
              "Running agents finish their current node; tick is notify-only.")
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
        ("disarm", "Disarm the armed project (global flag unchanged)"),
        ("on", "Global autopilot ON (flag cache only)"),
        ("off", "Disarm everything: clear armed project + global flag"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.set_defaults(func=cmd_autopilot, project=None)
    p_st = sub.add_parser("status", help="Show global flag + armed project + graph")
    p_st.add_argument("--json", dest="json_out", action="store_true")
    p_st.set_defaults(func=cmd_autopilot, project=None)
