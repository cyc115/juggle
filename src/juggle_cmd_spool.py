"""juggle_cmd_spool — `juggle spool` CLI: dead-letter recovery (RCA D1).

list-dead / replay / purge over spool_dead_dir(); thin argparse + printing
wrapper over juggle_spool_dead primitives. Recovery is a read+requeue surface
only — never the write/agent-context path.
"""
from __future__ import annotations

import json

from juggle_cli_common import get_db
from juggle_spool_dead import list_dead_entries, purge_dead, replay_dead


def cmd_spool_list_dead(args):
    entries = list_dead_entries()
    if getattr(args, "json_out", False):
        print(json.dumps(entries, indent=2))
        return
    if not entries:
        print("No dead-lettered spool events.")
        return
    print(f"{'UUID':<36}  {'TYPE':<18} {'CREATED_AT':<28} REASON")
    for e in entries:
        print(f"{e['uuid']:<36}  {e['type']:<18} {e['created_at']:<28} {e['dead_reason']}")
    print(f"\n{len(entries)} dead-lettered event(s) in spool/dead/.")


def cmd_spool_replay(args):
    all_ = getattr(args, "all", False)
    uuid = getattr(args, "uuid", None)
    if not all_ and not uuid:
        print("Error: pass a <uuid> (or unique prefix) or --all.")
        raise SystemExit(1)
    db = get_db(getattr(args, "db_path", None), init=True)
    results = replay_dead(db, uuid=uuid, all_=all_,
                          force_applying=getattr(args, "force_applying", False))
    if not results:
        print("No matching dead-lettered events.")
        return
    ok = 0
    for good, msg in results:
        if good:
            ok += 1
            print(f"  replayed {msg}")
        else:
            print(f"  SKIP {msg}")
    print(f"\nRe-injected {ok}/{len(results)} event(s) into the pending spool; "
          "the next watchdog drain will retry them.")


def cmd_spool_purge(args):
    before = getattr(args, "before", None)
    removed = purge_dead(before=before)
    scope = f" created before {before}" if before else ""
    print(f"Purged {removed} dead-lettered spool file(s){scope}.")


def register_spool_parsers(subparsers) -> None:
    """Register `juggle spool` and its list-dead/replay/purge subcommands."""
    p = subparsers.add_parser("spool", help="Spool dead-letter recovery (list/replay/purge)")
    sub = p.add_subparsers(dest="spool_command", required=True)

    p_list = sub.add_parser("list-dead", help="List dead-lettered spool events")
    p_list.add_argument("--json", dest="json_out", action="store_true", help="Machine-readable JSON")
    p_list.set_defaults(func=cmd_spool_list_dead)

    p_replay = sub.add_parser("replay", help="Re-inject a dead event into the pending spool")
    p_replay.add_argument("uuid", nargs="?", default=None,
                          help="UUID (or unique prefix) of the dead event")
    p_replay.add_argument("--all", action="store_true", help="Replay every dead event")
    p_replay.add_argument("--force-applying", dest="force_applying", action="store_true",
                          help="Override the 'applying'-state refusal (may double-apply side effects)")
    p_replay.set_defaults(func=cmd_spool_replay)

    p_purge = sub.add_parser("purge", help="Delete dead-lettered spool files")
    p_purge.add_argument("--before", default=None, metavar="YYYY-MM-DD",
                         help="Only delete files created before this date")
    p_purge.set_defaults(func=cmd_spool_purge)
