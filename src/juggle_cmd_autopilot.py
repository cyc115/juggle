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
    arm_project,
    disarm_project,
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


def _cmd_arm(db, project_id: str) -> None:
    from juggle_graph_status import format_progress, graph_counts

    project = db.get_project(project_id)
    if not project:
        print(f"Error: project {project_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    # PR-mode repos: verified would not mean merged (DA round-2 MAJOR-2,
    # 2026-06-10) — refuse to arm, same policy as project-graph load.
    from juggle_cmd_graph import pr_mode_refusal

    refusal = pr_mode_refusal()
    if refusal:
        print(f"Error: {refusal}", file=sys.stderr)
        sys.exit(1)
    try:
        armed = arm_project(db, project_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    _flag_set(True)  # arm while ON keeps ON — no rm-inversion (DA M6)
    spec = graphs_dir() / f"{project_id}-graph.md"
    try:
        graphs_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    counts = graph_counts(db, project_id)
    suffix = f" Armed set: {', '.join(armed)}." if len(armed) > 1 else ""
    print(f"AUTOPILOT ON — project {project_id} ({project['name']}) armed.{suffix}")
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


def _cmd_remove(db, project_id: str | None, *, clear_flag_when_empty: bool) -> None:
    """disarm/off: remove one project (or all), set-aware flag handling."""
    if project_id:
        if project_id not in get_armed_projects(db):
            print(f"Error: project {project_id!r} is not armed.", file=sys.stderr)
            sys.exit(1)
        remaining = disarm_project(db, project_id)
    else:
        set_armed_projects(db, [])
        remaining = []
    if clear_flag_when_empty and not remaining:
        _flag_set(False)
    rest = f" Still armed: {', '.join(remaining)}." if remaining else ""
    what = f"Project {project_id} disarmed." if project_id else "All projects disarmed."
    print(f"{what}{rest} Global autopilot: "
          f"{'ON' if AUTOPILOT_FLAG.exists() else 'OFF'}.")


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
    """Handler for `juggle autopilot <arm|disarm|on|off|status>`."""
    db = get_db(getattr(args, "db_path", None), init=True)
    cmd = args.autopilot_command
    if cmd == "arm":
        _cmd_arm(db, args.project)
    elif cmd == "disarm":
        _cmd_remove(db, getattr(args, "project", None), clear_flag_when_empty=False)
    elif cmd == "on":
        _flag_set(True)
        print("AUTOPILOT ON (global). No project armed — use: juggle autopilot arm <project>")
    elif cmd == "off":
        _cmd_remove(db, getattr(args, "project", None), clear_flag_when_empty=True)
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
