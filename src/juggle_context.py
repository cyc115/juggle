#!/usr/bin/env python3
"""Juggle Context - builds additionalContext for UserPromptSubmit hook."""

import threading

from juggle_cli_common import _humanize_dt, _extract_decision_prompt, _last_sentences
from juggle_db import JuggleDB, _is_junk_message, _thread_age_seconds

# Hard cap: ~2000 tokens => ~8000 chars
_CHAR_LIMIT = 8000

# Max chars for a non-current thread's summary teaser
_TEASER_CHARS = 80


def _build(db: JuggleDB) -> str:
    if not db.is_active():
        return ""

    parts: list[str] = []
    parts.append("--- JUGGLE ACTIVE (do not forward to sub-agents) ---")
    parts.append("RULE: Every Agent call MUST use run_in_background=true. No foreground agents ever.")

    # ------------------------------------------------------------------
    # ACTION REQUIRED — completed agent notifications (top of block)
    # ------------------------------------------------------------------
    notifications = db.get_pending_notifications()
    # Auto-clear notifications shown 3+ times without acknowledgement
    stale_ids = [n["id"] for n in notifications if (n.get("delivery_attempts") or 0) >= 3]
    if stale_ids:
        db.mark_notifications_delivered(stale_ids)
    notifications = [n for n in notifications if n["id"] not in stale_ids]
    completion_notifs = [n for n in notifications if "completed" in n["message"] or "results ready" in n["message"]]
    other_notifs = [n for n in notifications if n not in completion_notifs]

    if completion_notifs:
        parts.append("")
        parts.append("⚠️ ACTION REQUIRED — Tell user about completed agents BEFORE doing anything else:")
        for n in completion_notifs:
            attempts = n.get("delivery_attempts") or 0
            if attempts >= 3:
                parts.append(f"  🚨 UNACKNOWLEDGED (attempt {attempts}): {n['message']}")
            else:
                parts.append(f"  → {n['message']}")
        parts.append("After announcing completions to user, proceed with their request.")

    current_thread = db.get_current_thread()
    all_threads = [t for t in db.get_all_threads()
                   if t.get("status") != "archived" and t.get("show_in_list", 1) != 0]
    thread = None

    # ------------------------------------------------------------------
    # Current topic line
    # ------------------------------------------------------------------
    if current_thread:
        thread = db.get_thread(current_thread)
        if thread:
            display_label = thread.get("label") or current_thread[:8]
            parts.append(
                f"Current topic: [{display_label}] {thread['topic']}"
            )

    # ------------------------------------------------------------------
    # Topics table
    # Current thread: full summary + key decisions
    # Other threads: 1-line teaser (first 80 chars of summary)
    # ------------------------------------------------------------------
    if all_threads:
        parts.append("")
        parts.append("Topics:")
        for t in all_threads:
            tid = t["id"]
            label = t.get("label") or tid[:8]
            topic = t["topic"]
            status = t["status"]

            if tid == current_thread:
                suffix = " ← you are here"
            elif status == "background":
                suffix = " 🏃\u200d♂️ agent working..."
            elif status == "done":
                suffix = " ✓ done"
            elif status == "failed":
                suffix = " ✗ failed"
            elif status == "archived":
                suffix = " 🗄️ archived"
            else:
                suffix = ""

            parts.append(f"  [{label}] {topic}{suffix}")

            summary = t.get("summary", "").strip()
            if summary:
                if tid == current_thread:
                    # Full summary for the current thread
                    parts.append(f"    Summary: {summary}")
                else:
                    # 1-line teaser for non-current threads
                    teaser = summary[:_TEASER_CHARS]
                    if len(summary) > _TEASER_CHARS:
                        teaser = teaser.rstrip() + "…"
                    parts.append(f"    Summary: {teaser}")

    # ------------------------------------------------------------------
    # Stale summary flag
    # ------------------------------------------------------------------
    if current_thread and thread:
        msg_count = db.get_message_count(current_thread, exclude_junk=True)
        summarized_count = thread.get("summarized_msg_count") or 0
        delta = msg_count - summarized_count
        if delta >= 3:
            parts.append(
                f"[SUMMARY STALE: {delta} new messages — summarize after responding]"
            )

    # ------------------------------------------------------------------
    # Shared project context (decisions + facts only)
    # ------------------------------------------------------------------
    shared = [
        s for s in db.get_shared_context()
        if s["context_type"] in ("decision", "fact")
    ]
    if shared:
        parts.append("")
        parts.append("Shared project context:")
        for s in shared:
            src = f" (from Thread {s['source_thread']})" if s.get("source_thread") else ""
            parts.append(
                f"  [{s['context_type']}] {s['content']}{src}"
            )

    # ------------------------------------------------------------------
    # Pending notifications (non-completion only; completions shown at top)
    # ------------------------------------------------------------------
    if other_notifs:
        parts.append("")
        parts.append("Pending notifications:")
        for n in other_notifs:
            parts.append(
                f"  \u26a1 {n['message']}"
            )

    parts.append("--- END JUGGLE ---")

    result = "\n".join(parts)

    # Enforce character cap; trim from the middle (keep header + footer)
    if len(result) > _CHAR_LIMIT:
        result = _trim_to_limit(result, _CHAR_LIMIT)

    return result


def _trim_to_limit(text: str, limit: int) -> str:
    """
    Trim text to at most `limit` chars while preserving the header/footer lines
    and as much of the body as possible.
    """
    lines = text.splitlines()
    if not lines:
        return text

    header = lines[0]   # "--- JUGGLE ACTIVE (do not forward to sub-agents) ---"
    footer = lines[-1]  # "--- END JUGGLE ---"

    # Reserve space for header + footer + two newlines
    reserved = len(header) + len(footer) + 2
    body_budget = limit - reserved
    if body_budget <= 0:
        return f"{header}\n{footer}"

    body_lines = lines[1:-1]
    body = "\n".join(body_lines)

    if len(body) <= body_budget:
        return text

    # Keep as much of the body as fits
    cut = body[:body_budget - 4]
    cut = cut[:cut.rfind(' ')] if ' ' in cut else cut
    trimmed_body = cut + "\n..."
    return f"{header}\n{trimmed_body}\n{footer}"


# Backwards-compatible alias for tests and any external callers
class ContextBuilder:
    def __init__(self, db: JuggleDB):
        self.db = db

    def build(self) -> str:
        return _build(self.db)


def build_context_string(db_path=None) -> str:
    """
    Module-level convenience function for use by the UserPromptSubmit hook.

    Returns the juggle additionalContext string, or '' if juggle is inactive.
    Note: init_db() must be called at juggle start, not on every prompt.
    """
    db = JuggleDB(db_path=db_path)
    return _build(db)


def get_thread_state(db: JuggleDB, thread: dict, current_thread_id: str) -> str:
    """Return emoji state string for a thread dict.

    Returns one of: "👉", "🏃‍♂️", "⏸️", "💤", "✅", "❌", "🗄️", or "".
    Priority (highest wins): current > background > done > failed > archived > waiting > idle
    """
    tid = thread["id"]
    status = thread.get("status") or "active"
    last_active = thread.get("last_active") or ""

    # Current
    if tid == current_thread_id:
        return "👉"

    # Background (agent running)
    if status == "background":
        return "🏃\u200d♂️"

    # Done
    if status == "done":
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
                has_real_reply = any(not _is_junk_message(r["content"]) for r in user_rows)
                if not has_real_reply:
                    return "⏸️"
        return "✅"

    # Failed
    if status == "failed":
        return "❌"

    # Archived: last_active > 48 hours ago
    age = _thread_age_seconds(last_active)
    if age is not None and age > 48 * 3600:
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

    # Idle: last assistant message exists (no "?") AND last_active > 30 min ago
    if assistant_row and age is not None and age > 30 * 60:
        return "💤"

    return ""


# ---------------------------------------------------------------------------
# Cross-session resume helpers
# ---------------------------------------------------------------------------

def _get_juggle_version() -> str:
    import json as _json
    from pathlib import Path
    plugin_json = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return _json.loads(plugin_json.read_text())["version"]
    except Exception:
        return "?"


def _recall_for_thread(topic: str) -> list:
    """Return up to 2 Hindsight snippet lines for a topic. Empty list on any failure."""
    try:
        from juggle_hindsight import HindsightClient
        from juggle_cli_common import JUGGLE_CONFIG_PATH
        client = HindsightClient.from_config(str(JUGGLE_CONFIG_PATH))
        if client is None:
            return []
        raw = client.recall(topic, max_tokens=512)
        if not raw:
            return []
        lines = [ln.lstrip("- ").strip() for ln in raw.splitlines() if ln.strip()]
        return lines[:2]
    except Exception:
        return []


def render_topics_tree(db: JuggleDB, memories: "dict | None" = None) -> str:
    """Render the topics tree as a string.

    memories: optional map of thread_id → list of recall snippet strings (🧠 lines).
    Returns 'No topics.' if no visible threads.
    """
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
        last_active = t.get("last_active") or ""
        inverted = "".join(chr(0x10FFFF - ord(c)) for c in last_active) if last_active else ""
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
        label = t.get("label") or tid[:8]
        topic = t["topic"]
        title = t.get("title") or topic
        last_active = _humanize_dt(t.get("last_active") or "")

        emoji = get_thread_state(db, t, current or "")
        state_suffix = _state_suffix_text.get(emoji, "")

        header = f"{branch} {emoji} **[{label}] {title}**  ({last_active})"
        if state_suffix:
            header = f"{header}  {state_suffix}"
        output_lines.append(header)

        summary = (t.get("summary") or "").strip()
        summary_text = summary if summary else "no summary yet"
        output_lines.append(f"{vert}├── Summary: {summary_text}")

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

        # 🧠 Hindsight memories — injected after decisions, before open questions
        if memories:
            for snippet in memories.get(tid, []):
                if snippet.strip():
                    output_lines.append(f"{vert}├── 🧠 {snippet.strip()}")

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

        status = t["status"]
        if status == "background":
            agent_status = t.get("last_user_intent") or t.get("agent_task_id") or "running..."
            output_lines.append(f"{vert}├── ⏳ {agent_status}")

        if emoji == "⏸️":
            exchange = db.get_last_exchange(tid)
            decision = _extract_decision_prompt(
                exchange.get("last_assistant"),
                exchange.get("last_user"),
            )
            output_lines.append(f"{vert}└── {decision}")
        else:
            exchanges = db.get_recent_exchanges(tid, n=2)
            exchange_labels = ["Last:", "Prior:"]
            for ex_idx, exchange in enumerate(exchanges):
                ex_label = exchange_labels[ex_idx] if ex_idx < len(exchange_labels) else "     "
                user_text = _last_sentences(exchange.get("user") or "")
                asst_text = _last_sentences(exchange.get("assistant") or "")
                user_display = f'"{user_text}"' if user_text else "(none)"
                asst_display = f'"{asst_text}"' if asst_text else "(none)"
                connector = "└──" if ex_idx == len(exchanges) - 1 else "├──"
                output_lines.append(f"{vert}{connector} {ex_label} Q: {user_display}")
                output_lines.append(f"{vert}         A: {asst_display}")

        if not is_last:
            output_lines.append("│")

    output_lines.append("")
    output_lines.append('Use "/juggle:resume-topic <id>" to switch topics, or just keep talking.')

    return "\n".join(output_lines)


def build_startup_output(db: JuggleDB) -> str:
    """Full enriched startup string: topics tree + per-thread Hindsight recalls.

    Called by cmd_start (when threads exist) and handle_session_start (resume/compact).
    """
    # Lazy import to avoid circular dependency (juggle_context ← juggle_cmd_threads)
    from juggle_cmd_threads import _cleanup_orphaned_threads
    _cleanup_orphaned_threads(db)

    threads = db.get_all_threads()
    active = [
        t for t in threads
        if t.get("status") != "archived" and t.get("show_in_list", 1) != 0
    ]

    if not active:
        return "Juggle active. No open topics."

    # Parallel Hindsight recall — 2s timeout, failures silently swallowed
    memories: dict = {}
    lock = threading.Lock()

    def _fetch(t: dict) -> None:
        snippets = _recall_for_thread(t["topic"])
        if snippets:
            with lock:
                memories[t["id"]] = snippets

    recall_threads = [
        threading.Thread(target=_fetch, args=(t,), daemon=True)
        for t in active
    ]
    for rt in recall_threads:
        rt.start()
    for rt in recall_threads:
        rt.join(timeout=10)

    n = len(active)
    ver = _get_juggle_version()
    header = f"Juggle v{ver} active. Resuming {n} open topic{'s' if n != 1 else ''}:\n"
    return header + render_topics_tree(db, memories=memories)


if __name__ == "__main__":
    # Quick smoke-test / manual inspection
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_context_string(db_path=db_path))
