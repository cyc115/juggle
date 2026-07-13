"""juggle_watchdog_thread_label — shared thread-label lookup for watchdog log
and event messages (dedup of juggle_watchdog._get_thread_label and
juggle_watchdog_reap_done._thread_label, previously two copies of the same
five lines)."""
from __future__ import annotations

from typing import Any


def thread_label(db: Any, thread_id: str | None, fallback: str = "unknown") -> str:
    """user_label (or legacy label) for ``thread_id``, else its short id, else
    ``fallback`` when there's no thread id at all."""
    if not thread_id:
        return fallback
    thread = db.get_thread(thread_id)
    if not thread:
        return thread_id[:8]
    return thread.get("user_label") or thread.get("label") or thread_id[:8]
