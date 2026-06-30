"""juggle_cli_commands_threads — thread/session COMMANDS entries (P9 R3).

Ports juggle_cli_parsers_threads.register() 1:1 into declarative Cmd entries,
KEEPING legacy flat names as the canonical verb (resource=None — no rename until
G1). Handlers are the SAME objects the wall binds; ``doctor`` reuses the wall's
``_doctor_dispatch`` (exit-code propagation preserved). Data only — no wiring.
"""

from __future__ import annotations

from juggle_cli_spec import Arg, Cmd
from juggle_cmd_threads import (
    cmd_archive_thread,
    cmd_close_thread,
    cmd_create_thread,
    cmd_get_archive_candidates,
    cmd_get_messages,
    cmd_get_stale_threads,
    cmd_set_summarized_count,
    cmd_show_topics,
    cmd_start,
    cmd_stop,
    cmd_switch_thread,
    cmd_unarchive_thread,
    cmd_update_meta,
)


def _doctor_dispatch(a):
    """Run doctor and PROPAGATE its return code as the process exit code.

    The top-level CLI dispatch discards ``func``'s return value, so a bare
    ``cmd_doctor`` -> 1 would still exit 0. ``--pre-p8-check`` MUST exit nonzero
    until both gates clear, so forward the code here via ``sys.exit``. The normal
    doctor path returns 0 -> ``sys.exit(0)`` (unchanged observable behavior).

    Relocated here from juggle_cli_parsers_threads (P9 R4 deleted that wall).
    """
    import sys

    sys.exit(__import__("juggle_cmd_doctor").cmd_doctor(a) or 0)

THREAD_COMMANDS: tuple[Cmd, ...] = (
    Cmd(None, "start", cmd_start,
        args=(Arg("--session-id", dest="session_id", default=None),),
        help="Start juggle mode"),
    Cmd(None, "stop", cmd_stop, help="Stop juggle mode"),
    Cmd(None, "create-thread", cmd_create_thread,
        args=(Arg("topic", help="Topic name"),),
        help="Create a new topic thread"),
    Cmd(None, "doctor", _doctor_dispatch,
        args=(
            Arg("--dry-run", action="store_true", help="Print actions; write nothing"),
            Arg("--pre-p8-check", action="store_true", dest="pre_p8_check",
                help="Report remaining legacy-table refs (static) + nodes mirror "
                     "readiness (runtime); exit nonzero until both clear"),
            Arg("--json", action="store_true", dest="json_out",
                help="Emit --pre-p8-check result as JSON"),
        ),
        help="Migrate config + DB to current schema"),
    Cmd(None, "switch-thread", cmd_switch_thread,
        args=(Arg("thread_id", help="Thread ID (e.g. A, B, C)"),),
        help="Switch to a topic thread"),
    Cmd(None, "update-meta", cmd_update_meta,
        args=(
            Arg("thread_id", help="Thread ID"),
            Arg("--add-decision", dest="add_decision", default=None, metavar="TEXT"),
            Arg("--add-question", dest="add_question", default=None, metavar="TEXT"),
            Arg("--resolve-question", dest="resolve_question", default=None, metavar="TEXT"),
        ),
        help="Update thread metadata"),
    Cmd(None, "close-thread", cmd_close_thread,
        args=(Arg("thread_id", help="Thread ID"),), help="Close a thread"),
    Cmd(None, "show-topics", cmd_show_topics, help="Show all topics"),
    Cmd(None, "get-archive-candidates", cmd_get_archive_candidates,
        help="List threads that are candidates for archiving"),
    Cmd(None, "archive-thread", cmd_archive_thread,
        args=(Arg("thread_id", help="Thread ID to archive"),), help="Archive a thread"),
    Cmd(None, "unarchive-thread", cmd_unarchive_thread,
        args=(Arg("thread_id", help="Thread ID to unarchive (label or UUID)"),),
        help="Unarchive a thread"),
    Cmd(None, "set-summarized-count", cmd_set_summarized_count,
        args=(Arg("thread_id"), Arg("count", type=int)),
        help="Set summarized message count"),
    Cmd(None, "get-stale-threads", cmd_get_stale_threads,
        args=(Arg("--threshold", type=int, default=3),),
        help="List threads with stale summaries"),
    Cmd(None, "get-messages", cmd_get_messages,
        args=(
            Arg("thread_id"),
            Arg("--limit", type=int, default=None),
            Arg("--plain", action="store_true", help="Plain role: content format"),
        ),
        help="Show messages for a thread"),
)
