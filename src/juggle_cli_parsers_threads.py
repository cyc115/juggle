"""
juggle_cli_parsers_threads — Subparser registration for thread/session commands.

Owns: argparse wiring for start/stop/doctor and all thread lifecycle commands.
Must not own: command handler logic (lives in juggle_cmd_threads).
"""

from juggle_cmd_threads import (
    cmd_start,
    cmd_stop,
    cmd_create_thread,
    cmd_switch_thread,
    cmd_update_meta,
    cmd_close_thread,
    cmd_show_topics,
    cmd_get_archive_candidates,
    cmd_archive_thread,
    cmd_unarchive_thread,
    cmd_set_summarized_count,
    cmd_get_stale_threads,
    cmd_get_messages,
)


def _doctor_dispatch(a):
    """Run doctor and PROPAGATE its return code as the process exit code.

    The top-level CLI dispatch discards ``func``'s return value, so a bare
    ``cmd_doctor`` -> 1 would still exit 0. ``--pre-p8-check`` MUST exit nonzero
    until both gates clear (its return is the agent-verifiable readiness signal),
    so forward the code here via ``sys.exit``. The normal doctor path returns 0
    -> ``sys.exit(0)`` (unchanged observable behavior). ``cmd_doctor`` itself is
    unchanged, so direct callers/tests still observe the int return.
    """
    import sys

    sys.exit(__import__("juggle_cmd_doctor").cmd_doctor(a) or 0)


def register(subparsers) -> None:
    """Register thread/session subcommands on the given subparsers object."""
    # start
    p_start = subparsers.add_parser("start", help="Start juggle mode")
    p_start.add_argument("--session-id", dest="session_id", default=None)
    p_start.set_defaults(func=cmd_start)

    # stop
    p_stop = subparsers.add_parser("stop", help="Stop juggle mode")
    p_stop.set_defaults(func=cmd_stop)

    # create-thread
    p_create = subparsers.add_parser("create-thread", help="Create a new topic thread")
    p_create.add_argument("topic", help="Topic name")
    p_create.set_defaults(func=cmd_create_thread)

    # doctor — auto-migrate config + DB to current schema
    p_doctor = subparsers.add_parser(
        "doctor", help="Migrate config + DB to current schema"
    )
    p_doctor.add_argument(
        "--dry-run", action="store_true", help="Print actions; write nothing"
    )
    p_doctor.add_argument(
        "--pre-p8-check", action="store_true", dest="pre_p8_check",
        help="Report remaining legacy-table refs (static) + nodes mirror readiness "
             "(runtime); exit nonzero until both clear",
    )
    p_doctor.add_argument(
        "--json", action="store_true", dest="json_out",
        help="Emit --pre-p8-check result as JSON",
    )
    p_doctor.set_defaults(func=_doctor_dispatch)

    # switch-thread
    p_switch = subparsers.add_parser("switch-thread", help="Switch to a topic thread")
    p_switch.add_argument("thread_id", help="Thread ID (e.g. A, B, C)")
    p_switch.set_defaults(func=cmd_switch_thread)

    # update-meta
    p_meta = subparsers.add_parser("update-meta", help="Update thread metadata")
    p_meta.add_argument("thread_id", help="Thread ID")
    p_meta.add_argument(
        "--add-decision", dest="add_decision", default=None, metavar="TEXT"
    )
    p_meta.add_argument(
        "--add-question", dest="add_question", default=None, metavar="TEXT"
    )
    p_meta.add_argument(
        "--resolve-question", dest="resolve_question", default=None, metavar="TEXT"
    )
    p_meta.set_defaults(func=cmd_update_meta)

    # close-thread
    p_close = subparsers.add_parser("close-thread", help="Close a thread")
    p_close.add_argument("thread_id", help="Thread ID")
    p_close.set_defaults(func=cmd_close_thread)

    # show-topics
    p_show = subparsers.add_parser("show-topics", help="Show all topics")
    p_show.set_defaults(func=cmd_show_topics)

    # get-archive-candidates
    p_archive_candidates = subparsers.add_parser(
        "get-archive-candidates", help="List threads that are candidates for archiving"
    )
    p_archive_candidates.set_defaults(func=cmd_get_archive_candidates)

    # archive-thread
    p_archive = subparsers.add_parser("archive-thread", help="Archive a thread")
    p_archive.add_argument("thread_id", help="Thread ID to archive")
    p_archive.set_defaults(func=cmd_archive_thread)

    # unarchive-thread
    p_unarchive = subparsers.add_parser("unarchive-thread", help="Unarchive a thread")
    p_unarchive.add_argument("thread_id", help="Thread ID to unarchive (label or UUID)")
    p_unarchive.set_defaults(func=cmd_unarchive_thread)

    # set-summarized-count
    p_set_count = subparsers.add_parser(
        "set-summarized-count", help="Set summarized message count"
    )
    p_set_count.add_argument("thread_id")
    p_set_count.add_argument("count", type=int)
    p_set_count.set_defaults(func=cmd_set_summarized_count)

    # get-stale-threads
    p_stale = subparsers.add_parser(
        "get-stale-threads", help="List threads with stale summaries"
    )
    p_stale.add_argument("--threshold", type=int, default=3)
    p_stale.set_defaults(func=cmd_get_stale_threads)

    # get-messages
    p_msgs = subparsers.add_parser("get-messages", help="Show messages for a thread")
    p_msgs.add_argument("thread_id")
    p_msgs.add_argument("--limit", type=int, default=None)
    p_msgs.add_argument(
        "--plain", action="store_true", help="Plain role: content format"
    )
    p_msgs.set_defaults(func=cmd_get_messages)
