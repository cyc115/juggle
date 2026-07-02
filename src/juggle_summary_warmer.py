"""Eager background warming for the (i)-pane topic-summary cache.

The modal (`juggle_cockpit_modal_node._fetch_summary`) regenerates the summary
synchronously on a cache miss, which is what makes opening the modal feel slow
(3.9-18.3s per LLM call). `warm_stale_summaries` runs from the watchdog tick
instead: it regenerates any topic's cache row BEFORE the user opens the modal,
so the modal path becomes a cache-only read in the common case. Cost is
negligible (~3.2k in / ~400 out tokens per summary, measured 2026-07-01).
"""

from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger("juggle.watchdog")

# Safety cap on regens per sweep: a cold start (or a burst of simultaneously
# stale topics) must not block a single tick for minutes running sequential
# LLM calls (3.9-18.3s each, measured 2026-07-01) — backlog drains gradually
# across ticks instead.
_DEFAULT_MAX_REGENS_PER_SWEEP = 5


def topic_needs_warming(cached_last_message_id: int | None, current_cursor: int, threshold: int) -> bool:
    """Pure decision: should the eager warmer regenerate this topic's cache row?

    No cached row -> True (caller only invokes this once current_cursor > 0,
    i.e. the topic actually has messages). Cache exists -> True once the
    message cursor has advanced by >= threshold since the cached row, so the
    tick doesn't burn an LLM call on every single new message (debounce).
    """
    if cached_last_message_id is None:
        return True
    return (current_cursor - cached_last_message_id) >= threshold


def warm_stale_summaries(
    db, llm_fn=None, threshold: int | None = None, max_regens: int = _DEFAULT_MAX_REGENS_PER_SWEEP
) -> int:
    """Regenerate stale topic_summary_cache rows for non-archived topics.

    At most one regen per topic per call (single pass over `get_all_threads`),
    and at most `max_regens` total per call — remaining stale topics are left
    for the next tick sweep rather than blocking this one. Any per-topic
    failure is logged and skipped — one bad topic never aborts the sweep.
    Returns the number of topics regenerated.
    """
    if db is None:
        return 0

    from juggle_cockpit_modals import build_summary_ctx
    from juggle_settings import get_settings
    from juggle_topic_summary import summarize_topic
    from juggle_topic_summary_cache import (
        child_node_signature,
        current_cursor,
        read_summary_cache,
        store_summary,
    )

    limit = threshold if threshold is not None else int(get_settings()["stale_summary_message_threshold"])

    try:
        threads = [t for t in db.get_all_threads() if (t.get("state") or "") != "archived"]
    except Exception:
        _log.exception("warm_stale_summaries: get_all_threads failed")
        return 0

    regenerated = 0
    for thread in threads:
        if regenerated >= max_regens:
            _log.info(
                "warm_stale_summaries: hit max_regens=%d cap — remaining stale topics deferred to next sweep",
                max_regens,
            )
            break
        thread_id = thread.get("id")
        if not thread_id:
            continue
        try:
            with db._connect() as conn:
                conn.row_factory = sqlite3.Row
                cursor = current_cursor(conn, thread_id)
                if cursor == 0:
                    continue  # no messages yet — nothing to summarize
                cached = read_summary_cache(conn, thread_id)

            cached_last_message_id = cached["last_message_id"] if cached else None
            if not topic_needs_warming(cached_last_message_id, cursor, limit):
                continue

            ctx = build_summary_ctx(db, thread_id)
            node_sig = child_node_signature(ctx.get("child_nodes"))
            meta = {
                "label": thread.get("user_label") or "",
                "title": thread.get("title") or "",
                "status": thread.get("state") or "",
                "child_nodes": ctx.get("child_nodes") or [],
            }
            sections = summarize_topic(
                ctx.get("task_input", ""),
                ctx.get("result_output", ""),
                ctx.get("messages_all") or [],
                meta,
                llm_fn=llm_fn,
            )
            store_summary(db, thread_id, cursor, sections, {}, node_sig)
            regenerated += 1
        except Exception:
            _log.exception("warm_stale_summaries: failed for thread %s", thread_id)
            continue

    return regenerated
