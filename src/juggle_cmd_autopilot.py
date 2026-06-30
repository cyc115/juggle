"""juggle_cmd_autopilot — `juggle autopilot` arm/disarm/on/off/status.

Owns: the per-project arm/disarm surface (restored 2026-06-30, default-armed).
The settings-table key ``autopilot_disarmed_project`` is the exclusion-set
authority; an empty set means every active project is armed. ``disarm`` adds a
project to the set, ``arm`` removes it; ``arm``/``disarm`` with no id are
arm-all / disarm-all. ``~/.juggle/autopilot`` stays an existence-only cache for
the GLOBAL toggle (master kill switch) — on/off never touch the disarmed set,
and arm/disarm never touch the global flag.
Must not own: dispatching (juggle_graph_dispatch), graph load/validation
(juggle_cmd_graph), or hook injection (juggle_hooks_autopilot).
"""

from __future__ import annotations

import json
from pathlib import Path

from juggle_cli_common import get_db
from juggle_autopilot_state import (
    arm_all,
    arm_project,
    disarm_all,
    disarm_project,
    get_armed_projects,
    get_disarmed_projects,
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


def _cmd_off(db) -> None:
    """off: clear the GLOBAL autopilot flag (master kill). Does NOT touch the
    per-project disarmed set — disarm choices persist across global toggles."""
    _flag_set(False)
    print("AUTOPILOT OFF.")


def _cmd_arm(db, project: str | None) -> None:
    """arm <pid>: re-arm one project. arm (no arg): arm-all (clear disarm)."""
    if project:
        disarmed = arm_project(db, project)
        print(f"ARMED {project}. Disarmed set: {', '.join(disarmed) or '(none)'}")
    else:
        arm_all(db)
        print("ARMED ALL (disarmed set cleared).")


def _cmd_disarm(db, project: str | None) -> None:
    """disarm <pid>: exclude one project. disarm (no arg): disarm-all."""
    if project:
        disarmed = disarm_project(db, project)
        print(f"DISARMED {project}. Disarmed set: {', '.join(disarmed)}")
    else:
        all_ids = [p["id"] for p in db.list_projects()]
        disarm_all(db, all_ids)
        print(f"DISARMED ALL ({len(all_ids)} projects excluded).")


def _cmd_status(db, json_out: bool) -> None:
    from dbops.db_topics import topic_counts
    from juggle_graph_status import format_progress, graph_counts

    global_on = AUTOPILOT_FLAG.exists()
    disarmed = get_disarmed_projects(db)
    armed = get_armed_projects(db)
    graphs = {}
    for pid in armed:
        tc, nc = topic_counts(db, pid), graph_counts(db, pid)
        graphs[pid] = {"topics": tc, "tasks": nc} if (tc or nc) else None
    if json_out:
        print(json.dumps({
            "global_on": global_on,
            "disarmed_projects": disarmed,
            "armed_projects": armed,
            "graphs": graphs,
        }))
        return
    print(f"Autopilot global: {'ON' if global_on else 'OFF'}")
    print(f"Disarmed projects ({len(disarmed)}): {', '.join(disarmed) or '(none)'}")
    if not armed:
        print("Armed projects: (none active)")
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
    if not global_on and disarmed:
        print("NOTE: global autopilot is OFF — disarm choices take effect once "
              "you turn it ON.")


def cmd_autopilot(args) -> None:
    """Handler for `juggle autopilot <arm|disarm|on|off|status>`."""
    cmd = args.autopilot_command
    db = get_db(getattr(args, "db_path", None), init=True)
    project = getattr(args, "project", None)
    if cmd == "arm":
        _cmd_arm(db, project)
    elif cmd == "disarm":
        _cmd_disarm(db, project)
    elif cmd == "on":
        _flag_set(True)
        print("AUTOPILOT ON (global). The tick drives all ARMED projects.")
    elif cmd == "off":
        _cmd_off(db)
    else:
        _cmd_status(db, getattr(args, "json_out", False))


def register(subparsers) -> None:
    """Register the `autopilot` subcommand tree on the main CLI parser."""
    p = subparsers.add_parser(
        "autopilot",
        help="Per-project arm/disarm (default-armed exclusion set) + global toggle",
    )
    sub = p.add_subparsers(dest="autopilot_command", required=True)
    for name, hlp in (
        ("arm", "Re-arm a project (or arm-all with no id) — removes it from the disarmed set"),
        ("disarm", "Disarm a project (or disarm-all with no id) — adds it to the disarmed set"),
        ("on", "Global autopilot ON (flag cache only)"),
        ("off", "Global autopilot OFF (master kill; disarm set preserved)"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("project", nargs="?", default=None, help="Project id (optional)")
        sp.set_defaults(func=cmd_autopilot)
    p_st = sub.add_parser("status", help="Show global flag + disarmed + armed projects + graphs")
    p_st.add_argument("--json", dest="json_out", action="store_true")
    p_st.set_defaults(func=cmd_autopilot, project=None)
