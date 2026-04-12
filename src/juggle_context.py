#!/usr/bin/env python3
"""Juggle Context - builds additionalContext for UserPromptSubmit hook."""

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
    all_threads = db.get_all_threads()
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


if __name__ == "__main__":
    # Quick smoke-test / manual inspection
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_context_string(db_path=db_path))
