"""
juggle_cmd_misc — Miscellaneous CLI command handlers.

Owns: cmd_cockpit (TUI launcher + smoke harness), cmd_agent_tools
      (deny-block right-sizing report), and the selfheal subcommands.
Must not own: parser wiring (juggle_cli_parsers_*), thread/agent/project
command handlers (their own juggle_cmd_* modules).
"""

import sys
from pathlib import Path

from juggle_cli_common import get_db


def cmd_cockpit(args):
    """Launch the Juggle Cockpit dashboard (Textual, mouse drag-to-resize).

    With --out: render all panes as plain text to stdout then exit (no TUI).
    With --profile: run headless resource-usage profiling harness (no TUI).
    With --smoke: run viewport layout smoke test matrix (pty+pyte harness).
    """
    if getattr(args, "smoke", False):
        import json as _json
        from juggle_smoke import load_viewports, run_smoke

        vp_path = Path(__file__).parent.parent / "config" / "viewports.yaml"
        viewports = load_viewports(vp_path)

        viewport_name = getattr(args, "viewport_name", None)
        if viewport_name:
            if viewport_name not in viewports:
                print(
                    f"Unknown viewport: {viewport_name!r}. "
                    f"Available: {sorted(viewports)}",
                    file=sys.stderr,
                )
                sys.exit(1)
            viewports = {viewport_name: viewports[viewport_name]}

        out_dir = Path("data/cockpit-viewport-review")
        interactive = getattr(args, "interactive", False)
        results = run_smoke(
            viewports,
            db_path=getattr(args, "db_path", None),
            output_dir=out_dir,
            interactive=interactive,
            graph_mode=getattr(args, "smoke_graph", False),
        )

        any_fail = any(not r.get("pass") for r in results)
        if getattr(args, "json_out", False):
            print(_json.dumps(results, indent=2))
        else:
            for r in results:
                status = "PASS" if r.get("pass") else "FAIL"
                dims = f"{r['cols']}x{r['rows']}"
                err = r.get("error", "")
                trunc = r.get("truncation", {}).get("count", 0)
                trunc_str = f" (truncations:{trunc})" if trunc else ""
                print(f"{status}  {r['profile']:12s}  {dims:8s}{trunc_str}  {err}")
        sys.exit(1 if any_fail else 0)

    import subprocess as _sp
    src = Path(__file__).parent
    script = src / "juggle_cockpit.py"
    cmd = ["uv", "run", str(script)]
    if getattr(args, "db_path", None):
        cmd += ["--db", args.db_path]
    if getattr(args, "out", False):
        cmd += ["--out"]
    elif getattr(args, "screenshot", None):
        cmd += ["--screenshot", args.screenshot]
    elif getattr(args, "profile", False):
        cmd += ["--profile", "--duration", str(getattr(args, "duration", 60))]
    sys.exit(_sp.call(cmd))


def _deny_matches(tool_name: str, deny_list) -> bool:
    """True if tool_name is covered by a deny entry (exact or `prefix*` wildcard)."""
    for entry in deny_list or []:
        if entry.endswith("*"):
            if tool_name.startswith(entry[:-1]):
                return True
        elif tool_name == entry:
            return True
    return False


def cmd_agent_tools(args):
    """Report per-agent tool usage to systematically right-size the deny block.

    For each role it lists what tools the role actually used (with counts), and
    cross-references against that role's CONFIGURED deny to surface the two
    signals you need to tune the block:
      * over-aggressive  — a tool the role USED but its deny list strips (only
        visible from audit-mode runs); candidate to ALLOW.
      * too-loose        — a tool other roles use that this role never does and
        isn't denied; candidate to DENY.
    """
    import juggle_agent_settings as jas

    db = get_db(getattr(args, "db_path", None), init=True)

    if getattr(args, "reset", False):
        n = db.reset_agent_tool_usage()
        print(f"Cleared {n} agent tool-usage row(s).")
        return

    rows = db.get_agent_tool_usage(getattr(args, "role", None))
    if not rows:
        print(
            "No agent tool usage recorded yet.\n"
            "Dispatch agents (set agent.audit_mode=true first to relax per-role\n"
            "denies and measure true demand), then re-run this report."
        )
        return

    by_role: dict[str, list[dict]] = {}
    for r in rows:
        by_role.setdefault(r["role"], []).append(r)
    # Universe of tools any role used — proxy for "available" tools, since a
    # stripped tool never appears here.
    universe = {r["tool_name"] for r in rows}

    print("Agent tool usage  (mode: normal=steady-state, audit=denies relaxed)")
    for role in sorted(by_role):
        used = by_role[role]
        used_names = {u["tool_name"] for u in used}
        try:
            deny = (jas.build_agent_overlay(role).get("permissions") or {}).get("deny") or []
        except Exception:
            deny = []

        print(f"\n── {role} ──")
        for u in used:
            flag = ""
            if _deny_matches(u["tool_name"], deny):
                flag = "  ⚠ denied for this role but used → consider ALLOWING"
            sample = f"   {u['last_input']}" if u["last_input"] else ""
            print(f"  {u['tool_name']:<38} x{u['count']:<5} ({u['mode']}){flag}{sample}")

        candidates = sorted(
            t for t in universe if t not in used_names and not _deny_matches(t, deny)
        )
        if candidates:
            print("  candidates to DENY (used by other roles, never by this one):")
            for t in candidates:
                print(f"    {t}")


def _cmd_list_selfheal(args):
    import json as _json
    from pathlib import Path as _Path
    db = get_db(getattr(args, "db_path", None), init=True)
    rows = db.get_open_error_events()
    if getattr(args, "json", False):
        print(_json.dumps(rows, default=str))
        return
    if not rows:
        print("No pending self-heal errors.")
        return
    for row in rows:
        sig8 = (row["signature_hash"] or "")[:8]
        cls = row["error_class"]
        status = row["status"]
        count = row["count"]
        last = (row["last_seen"] or "")[:16]
        if cls == "A":
            detail = f"{row['exc_type'] or '?'} in {row['entrypoint'] or '?'}"
        else:
            ref = _Path(row["juggle_ref"] or "").name or row["juggle_ref"] or "?"
            detail = f"{row['entrypoint'] or '?'} error via {ref}"
        print(f"{row['id']:>4}  [{cls}]  {status:<20} count={count}  last={last}  sig={sig8}  {detail}")


def _cmd_selfheal_set_status(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    valid = ("open", "diagnosing", "awaiting_approval", "resolved")
    if args.status not in valid:
        print(f"error: invalid status {args.status!r}; choose from {valid}")
        sys.exit(1)
    updated = db.set_error_event_status(args.id, args.status, action_item_id=args.action_item_id)
    if updated:
        print(f"error_event {args.id} status → {args.status}")
    else:
        print(f"error: row {args.id} not found")
        sys.exit(1)


def _cmd_selfheal_reset_diagnosing(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status FROM error_events WHERE id = ?", (args.id,)
        ).fetchone()
    if not row:
        print(f"error: row {args.id} not found")
        sys.exit(1)
    if row["status"] != "diagnosing":
        print(f"error: row {args.id} not in diagnosing state (current: {row['status']})")
        sys.exit(1)
    db.set_error_event_status(args.id, "open")
    print(f"reset error_event {args.id} diagnosing→open")
