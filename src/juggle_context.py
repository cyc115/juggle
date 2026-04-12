#!/usr/bin/env python3
"""Juggle Context - builds additionalContext for UserPromptSubmit hook."""

from juggle_db import JuggleDB

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
    # Pending notifications
    # ------------------------------------------------------------------
    notifications = db.get_pending_notifications()
    if notifications:
        parts.append("")
        parts.append("Pending notifications:")
        for n in notifications:
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


if __name__ == "__main__":
    # Quick smoke-test / manual inspection
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_context_string(db_path=db_path))
