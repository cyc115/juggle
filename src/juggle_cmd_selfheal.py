"""
juggle_cmd_selfheal — CLI command handlers for the self-heal error_events family.

Owns: list-selfheal (grouped/flat), show-selfheal, selfheal-audit,
selfheal-set-status, selfheal-reset-diagnosing, selfheal-propose-nonissue.
Extracted from juggle_cmd_misc (2026-06-21, selfheal-triage-v2 P2) to keep that
module under the ≤300-line architecture gate — a domain seam mirroring
juggle_cli_parsers_selfheal.
Must not own: argparse wiring (juggle_cli_parsers_selfheal).
"""

import sys

from juggle_cli_common import get_db


def _selfheal_row_detail(row) -> str:
    from pathlib import Path as _Path
    if row.get("error_class") == "A":
        return f"{row.get('exc_type') or '?'} in {row.get('entrypoint') or '?'}"
    ref = _Path(row.get("juggle_ref") or "").name or row.get("juggle_ref") or "?"
    return f"{row.get('entrypoint') or '?'} error via {ref}"


def _print_selfheal_flat_row(row, indent: str = "") -> None:
    sig8 = (row.get("signature_hash") or "")[:8]
    st = row.get("status") or "?"
    last = (row.get("last_seen") or "")[:16]
    # Grey proposed-benign rows so they read as low-priority in the default view.
    prefix = "\033[2m" if st == "non_issue_proposed" else ""
    suffix = "\033[0m" if prefix else ""
    print(f"{prefix}{indent}{row.get('id'):>4}  [{row.get('error_class')}]  {st:<20} "
          f"count={row.get('count')}  last={last}  sig={sig8}  "
          f"{_selfheal_row_detail(row)}{suffix}")


def _cmd_list_selfheal(args):
    import json as _json
    db = get_db(getattr(args, "db_path", None), init=True)
    status = getattr(args, "status", None)
    include_hidden = getattr(args, "all", False)
    if getattr(args, "json", False):
        # Flat rows incl. group_key (get_open_error_events does SELECT *).
        rows = db.get_open_error_events(status=status, include_hidden=include_hidden)
        print(_json.dumps(rows, default=str))
        return
    if getattr(args, "flat", False):
        rows = db.get_open_error_events(status=status, include_hidden=include_hidden)
        if not rows:
            print("No matching self-heal errors.")
            return
        for row in rows:
            _print_selfheal_flat_row(row)
        return
    # Default: grouped view (group_key collapses line-drift; broad groups re-split).
    groups = db.get_grouped_error_events(status=status, include_hidden=include_hidden)
    if not groups:
        print("No matching self-heal errors.")
        return
    for g in groups:
        rep = g["representative"]
        cls = rep.get("error_class", "?")
        broad = "  ⚠BROAD→re-split" if g["broad"] else ""
        print(f"G {g['group_key'][:8]}  [{cls}]  {g['total_count']} occ / "
              f"{g['distinct_signatures']} variants{broad}  {_selfheal_row_detail(rep)}")
        # Re-split: surface every member signature of an over-aggregated group.
        if g["broad"] and g["members"]:
            for m in g["members"]:
                _print_selfheal_flat_row(m, indent="    ")


def _cmd_selfheal_audit(args):
    import json as _json
    db = get_db(getattr(args, "db_path", None), init=True)
    rows = db.get_selfheal_audit(limit=getattr(args, "limit", 50),
                                 action=getattr(args, "action", None))
    if getattr(args, "json", False):
        print(_json.dumps(rows, default=str))
        return
    if not rows:
        print("No self-heal audit rows.")
        return
    for r in rows:
        sig8 = (r.get("signature_hash") or "")[:8]
        gk8 = (r.get("group_key") or "")[:8]
        print(f"{(r.get('ts') or '')[:16]}  {r.get('action'):<16} "
              f"sig={sig8} grp={gk8} reason={r.get('reason') or ''}")


def _cmd_selfheal_set_status(args):
    from dbops.schema import VALID_ERROR_STATUSES
    db = get_db(getattr(args, "db_path", None), init=True)
    if args.status not in VALID_ERROR_STATUSES:
        print(f"error: invalid status {args.status!r}; "
              f"choose from {sorted(VALID_ERROR_STATUSES)}")
        sys.exit(1)
    updated = db.set_error_event_status(args.id, args.status, action_item_id=args.action_item_id)
    if updated:
        print(f"error_event {args.id} status → {args.status}")
    else:
        print(f"error: row {args.id} not found")
        sys.exit(1)


def _cmd_selfheal_propose_nonissue(args):
    db = get_db(getattr(args, "db_path", None), init=True)
    # Route through the gated benign-verdict seam so the silent_autohide config
    # gate is enforced in CODE (default: visible proposal). The gate stays OFF
    # until an operator opts in after watching the audit log.
    from juggle_settings import get_settings
    from juggle_selfheal_diagnosis import apply_benign_verdict
    with db._connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM error_events WHERE id = ?", (args.id,)).fetchone()
    if not exists:
        print(f"error: row {args.id} not found")
        sys.exit(1)
    status = apply_benign_verdict(db, args.id, get_settings().get("selfheal", {}))
    if status == "non_issue":
        print(f"error_event {args.id} status → non_issue (audited silent auto-hide, leased)")
    else:
        print(f"error_event {args.id} status → non_issue_proposed (awaiting operator confirm)")


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


def _cmd_show_selfheal(args):
    """Print one error_event's full triage detail (command_args + traceback +
    status + counts). Single-entry complement to list-selfheal."""
    import json as _json
    db = get_db(getattr(args, "db_path", None), init=True)
    row = db.get_error_event(args.id)
    if row is None:
        print(f"error: error_event {args.id} not found")
        sys.exit(1)
    if getattr(args, "json", False):
        print(_json.dumps(row, default=str))
        return
    sig = row["signature_hash"] or ""
    print(f"error_event {row['id']}  [class {row['error_class']}]  status={row['status']}")
    print(f"  signature : {sig}")
    print(f"  exc_type  : {row['exc_type'] or '-'}")
    print(f"  entrypoint: {row['entrypoint'] or '-'}")
    print(f"  surface   : {row['surface'] or '-'}")
    print(f"  juggle_ref: {row['juggle_ref'] or '-'}")
    print(f"  count     : {row['count']}")
    print(f"  first_seen: {row['first_seen']}")
    print(f"  last_seen : {row['last_seen']}")
    print(f"  action_item_id: {row['action_item_id'] if row['action_item_id'] is not None else '-'}")
    print(f"  command_args:\n    {row['command_args'] or '-'}")
    print("  traceback:")
    tb = row["traceback"] or "-"
    for line in tb.splitlines() or [tb]:
        print(f"    {line}")
