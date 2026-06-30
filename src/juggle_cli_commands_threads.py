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
    Cmd("thread", "create", cmd_create_thread,
        args=(Arg("topic", help="Topic name"),),
        aliases=("create-thread",),
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
    Cmd("thread", "switch", cmd_switch_thread,
        args=(Arg("thread_id", help="Thread ID (e.g. A, B, C)"),),
        aliases=("switch-thread",),
        help="Switch to a topic thread"),
    Cmd("thread", "update", cmd_update_meta,
        args=(
            Arg("thread_id", help="Thread ID"),
            Arg("--add-decision", dest="add_decision", default=None, metavar="TEXT"),
            Arg("--add-question", dest="add_question", default=None, metavar="TEXT"),
            Arg("--resolve-question", dest="resolve_question", default=None, metavar="TEXT"),
        ),
        aliases=("update-meta",),
        help="Update thread metadata"),
    Cmd("thread", "close", cmd_close_thread,
        args=(Arg("thread_id", help="Thread ID"),), aliases=("close-thread",),
        help="Close a thread"),
    Cmd("thread", "list", cmd_show_topics, aliases=("show-topics",),
        help="Show all topics"),
    Cmd("thread", "archive-candidates", cmd_get_archive_candidates,
        aliases=("get-archive-candidates",),
        help="List threads that are candidates for archiving"),
    Cmd("thread", "archive", cmd_archive_thread,
        args=(Arg("thread_id", help="Thread ID to archive"),),
        aliases=("archive-thread",), help="Archive a thread"),
    Cmd("thread", "unarchive", cmd_unarchive_thread,
        args=(Arg("thread_id", help="Thread ID to unarchive (label or UUID)"),),
        aliases=("unarchive-thread",), help="Unarchive a thread"),
    Cmd("thread", "set-summarized-count", cmd_set_summarized_count,
        args=(Arg("thread_id"), Arg("count", type=int)),
        aliases=("set-summarized-count",),
        help="Set summarized message count"),
    Cmd("thread", "list-stale", cmd_get_stale_threads,
        args=(Arg("--threshold", type=int, default=3),),
        aliases=("get-stale-threads",),
        help="List threads with stale summaries"),
    Cmd("thread", "messages", cmd_get_messages,
        args=(
            Arg("thread_id"),
            Arg("--limit", type=int, default=None),
            Arg("--plain", action="store_true", help="Plain role: content format"),
        ),
        aliases=("get-messages",),
        help="Show messages for a thread"),
)
