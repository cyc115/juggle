"""juggle_context_startup — topics tree, thread-state badges, startup output.

Owns: get_thread_state (emoji badge logic), render_topics_tree,
_auto_archive_closed_threads, build_startup_output, and their helpers —
the cross-session resume/startup rendering surface.
Must not own: the UserPromptSubmit additionalContext builder (juggle_context).
All names are re-exported from juggle_context for backward compatibility.
"""

import re

from juggle_cli_common import _humanize_dt, _extract_decision_prompt
from juggle_db import JuggleDB, _is_junk_message, _thread_age_seconds
from juggle_settings import get_settings as _get_settings


def get_thread_state(db: JuggleDB, thread: dict, current_thread_id: str) -> str:
    """Return emoji state string for a thread dict.

    Returns one of: "👉", "🏃‍♂️", "⏸️", "💤", "✅", "❌", "🗄️", or "".
    Priority (highest wins): current > background > done > failed > archived > waiting > idle
    """
    tid = thread["id"]
    state = thread.get("state") or "open"
    last_active = thread.get("last_active_at") or ""

    # Current
    if tid == current_thread_id:
        return "👉"

    # Background (agent running)
    if state == "background":
        return "🏃\u200d♂️"

    # Done (terminal: node 'done' covers legacy closed+done)
    if state == "done":
        with db._connect() as conn:
            asst_row = conn.execute(
                "SELECT id, content FROM messages WHERE thread_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if asst_row and asst_row["content"].rstrip().endswith("?"):
                user_rows = conn.execute(
                    "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' AND id > ? ORDER BY id ASC",
                    (tid, asst_row["id"]),
                ).fetchall()
                has_real_reply = any(
                    not _is_junk_message(r["content"]) for r in user_rows
                )
                if not has_real_reply:
                    return "⏸️"
        return "✅"

    # Failed
    if state == "failed-exec":
        return "❌"

    # Archived: last_active > archive threshold
    age = _thread_age_seconds(last_active)
    if age is not None and age > _get_settings()["thread_archive_threshold_secs"]:
        return "🗄️"

    # For waiting / idle detection we need the last assistant message
    with db._connect() as conn:
        assistant_row = conn.execute(
            """
            SELECT role, content, created_at FROM messages
            WHERE thread_id = ? AND role = 'assistant'
            ORDER BY id DESC LIMIT 1
            """,
            (tid,),
        ).fetchone()

    # Waiting: last message role == assistant AND content ends with "?"
    if assistant_row:
        if assistant_row["content"].rstrip().endswith("?"):
            return "⏸️"

    # Idle: last assistant message exists (no "?") AND last_active > idle threshold
    if (
        assistant_row
        and age is not None
        and age > _get_settings()["thread_idle_threshold_secs"]
    ):
        return "💤"

    return ""


# ---------------------------------------------------------------------------
# Cross-session resume helpers
# ---------------------------------------------------------------------------


def _get_juggle_version() -> str:
    import json as _json
    from pathlib import Path

    plugin_json = (
        Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    )
    try:
        return _json.loads(plugin_json.read_text())["version"]
    except Exception:
        return "?"



def render_topics_tree(db: JuggleDB) -> str:
    """Render the topics tree as a string. Returns 'No topics.' if no visible threads."""
    import json as _json

    threads = db.get_all_threads()
    if not threads:
        return "No topics."

    current = db.get_current_thread()

    # Filter archived / hidden threads
    threads = [t for t in threads if t.get("show_in_list", 1) != 0]
    if not threads:
        return "No topics."

    def _full_sort_key(t: dict) -> tuple:
        tid = t["id"]
        emoji = get_thread_state(db, t, current or "")
        if emoji == "⏸️":
            tier = 0
        elif emoji == "🏃\u200d♂️":
            tier = 1
        elif tid == (current or ""):
            tier = 2
        elif emoji in ("💤", "✅", "❌"):
            tier = 3
        else:
            tier = 2
        last_active = t.get("last_active_at") or ""
        inverted = (
            "".join(chr(0x10FFFF - ord(c)) for c in last_active) if last_active else ""
        )
        return (tier, inverted)

    threads.sort(key=_full_sort_key)

    _state_suffix_text = {
        "👉": "← YOU ARE HERE",
        "🏃\u200d♂️": "agent running",
        "⏸️": "waiting for you",
        "💤": "idle",
        "✅": "done",
        "❌": "failed",
        "🗄️": "archived",
        "": "",
    }

    output_lines = ["Topics"]
    last_idx = len(threads) - 1
    for idx, t in enumerate(threads):
        is_last = idx == last_idx
        branch = "└──" if is_last else "├──"
        vert = "    " if is_last else "│   "

        tid = t["id"]
        label = t.get("user_label") or t.get("label") or tid[:8]
        title = t.get("title") or "(untitled)"
        last_active = _humanize_dt(t.get("last_active_at") or "")

        emoji = get_thread_state(db, t, current or "")
        state_suffix = _state_suffix_text.get(emoji, "")

        header = f"{branch} {emoji} **[{label}] {title}**  ({last_active})"
        if state_suffix:
            header = f"{header}  {state_suffix}"
        output_lines.append(header)

        key_decisions_raw = t.get("key_decisions") or "[]"
        if isinstance(key_decisions_raw, str):
            try:
                key_decisions = _json.loads(key_decisions_raw)
            except (_json.JSONDecodeError, ValueError):
                key_decisions = []
        else:
            key_decisions = key_decisions_raw
        for decision in key_decisions:
            output_lines.append(f"{vert}├── ✅ {decision}")

        open_questions_raw = t.get("open_questions") or "[]"
        if isinstance(open_questions_raw, str):
            try:
                open_questions = _json.loads(open_questions_raw)
            except (_json.JSONDecodeError, ValueError):
                open_questions = []
        else:
            open_questions = open_questions_raw
        for question in open_questions:
            output_lines.append(f"{vert}├── ❓ {question}")

        state = t["state"]
        if state == "background":
            agent_status = (
                t.get("last_user_intent") or t.get("agent_task_id") or "running..."
            )
            output_lines.append(f"{vert}├── ⏳ {agent_status}")

        if emoji == "⏸️":
            exchange = db.get_last_exchange(tid)
            decision = _extract_decision_prompt(
                exchange.get("last_assistant"),
                exchange.get("last_user"),
            )
            output_lines.append(f"{vert}└── {decision}")

        if not is_last:
            output_lines.append("│")

    return "\n".join(output_lines)


def _auto_archive_closed_threads(db: JuggleDB) -> int:
    """Archive any closed thread whose last_active_at exceeds the TTL.

    Returns count of threads archived.
    """
    from datetime import datetime, timezone, timedelta

    ttl_secs = int(
        db.get_setting("thread_auto_archive_ttl_secs", default="3600") or "3600"
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_secs)
    archived = 0
    for t in db.get_threads_by_status("done"):
        la = t.get("last_active_at") or ""
        try:
            dt = datetime.strptime(la[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if dt < cutoff:
            db.archive_thread(t["id"])  # preserves user_label
            archived += 1
    return archived


def build_startup_output(db: JuggleDB) -> str:
    """Full enriched startup string: topics tree.

    Called by cmd_start (when threads exist) and handle_session_start (resume/compact).
    """
    # Lazy import to avoid circular dependency (juggle_context ← juggle_cmd_threads)
    from juggle_cmd_threads import _cleanup_orphaned_threads

    _cleanup_orphaned_threads(db)
    _auto_archive_closed_threads(db)

    threads = db.get_all_threads()
    active = [
        t
        for t in threads
        if t.get("state") != "archived" and t.get("show_in_list", 1) != 0
    ]

    if not active:
        return "Juggle active. No open topics."

    n = len(active)
    ver = _get_juggle_version()
    header = f"Juggle v{ver} active. Resuming {n} open topic{'s' if n != 1 else ''}:\n"
    return header + render_topics_tree(db)
