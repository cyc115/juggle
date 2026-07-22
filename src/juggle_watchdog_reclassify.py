"""juggle_watchdog_reclassify — async LLM retroactive-reclassify sweep.

Sits on top of the sync-local in-hook router (juggle_hooks_prompt): that
router is free, immediate, switch-only-on-confident-match, and never
auto-creates. THIS sweep is the periodic cheap-LLM cleanup pass — it re-files
mis-placed messages and MAY create a new topic, both fail-safe toward
*leaving messages put*. Owned by the watchdog tick (triage ladder: watchdog =
playbooks) — see research/2026-07-22-async-reclassify-spec.md for the full
design and the resolved forks (settings-key watermark, per-run granularity,
open-candidates-only re-file, current_thread advance OFF by default).

Must not own: message storage (dbops.messages.move_messages), thread creation
(dbops.threads.create_thread), or the sync router (juggle_hooks_prompt).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from dbops.schema import _is_junk_message, is_auto_topic_eligible
from juggle_hooks_prompt import get_classification_candidates

_log = logging.getLogger("juggle-watchdog")

# Cadence + safety thresholds (spec Q2/Q4/Q5).
RECLASSIFY_INTERVAL_S = 120
SETTLE_WINDOW_S = 20
MOVE_CONFIDENCE = 0.85
CREATE_CONFIDENCE = 0.80
MIN_WORDS_FOR_NEW_TOPIC = 6
BATCH_CAP = 50
FAILURE_BACKOFF_CAP = 5

_WATERMARK_KEY = "async_reclassify_watermark"
_AT_KEY = "async_reclassify_at"
_FAIL_KEY = "async_reclassify_fail_count"
# Default-off CAS advance flag (spec Q4 open question — ship OFF, confirm later).
_ADVANCE_FLAG_KEY = "async_reclassify_advance_current_thread"


def build_classify_prompt(run: list[dict], candidates: list[dict], db) -> str:
    """Build the classify prompt: open-thread dashboard + one message run. Pure
    modulo `db.get_last_exchange` (a read, no mutation)."""
    rows = []
    for c in candidates:
        last = db.get_last_exchange(c["id"])
        snippet = (last.get("last_user") or last.get("last_assistant") or "")[:120]
        rows.append(
            f"{c['id']} | {c.get('user_label') or '?'} | {c.get('title') or ''} | {snippet}"
        )
    dashboard = "\n".join(rows) or "(no open threads)"
    msg_lines = "\n".join(f"USER: {m['content'][:300]}" for m in run)
    return (
        "You are a background message classifier for a dev-orchestration tool.\n"
        "Below is a dashboard of OPEN conversation threads and a short run of "
        "consecutive user messages currently filed on one thread. Decide whether "
        "the run belongs where it is (stay), fits an existing OPEN thread better "
        "(move), or is the start of an unrelated new topic (new).\n\n"
        f"Open threads (id | label | title | last message):\n{dashboard}\n\n"
        f"Message run:\n{msg_lines}\n\n"
        "Reply with STRICT JSON only, no prose, no markdown fence:\n"
        '{"decision": "stay|move|new", "thread_id": "<dest id, required iff move, else null>", '
        '"confidence": 0.0, "topic_hint": "<short title, only iff new, else null>"}'
    )


def parse_classify_response(text: str | None) -> dict:
    """Parse the LLM's JSON verdict. Any parse failure / unknown shape falls
    back to a fail-safe 'stay' (spec Q3/Q5 — never invent a risky decision)."""
    fallback = {"decision": "stay", "thread_id": None, "confidence": 0.0, "topic_hint": None}
    if not text:
        return fallback
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    decision = data.get("decision")
    if decision not in ("stay", "move", "new"):
        return fallback
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    thread_id = data.get("thread_id")
    if not isinstance(thread_id, str):
        thread_id = None
    topic_hint = data.get("topic_hint")
    if not isinstance(topic_hint, str):
        topic_hint = None
    return {
        "decision": decision,
        "thread_id": thread_id,
        "confidence": confidence,
        "topic_hint": topic_hint,
    }


def _age_seconds(created_at: str, now: float) -> float | None:
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return now - dt.timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def _group_into_runs(rows: list[dict]) -> list[list[dict]]:
    """Group consecutive same-thread candidate messages into one run each
    (spec Q3: per-run classify granularity)."""
    runs: list[list[dict]] = []
    for row in rows:
        if runs and runs[-1][-1]["thread_id"] == row["thread_id"]:
            runs[-1].append(row)
        else:
            runs.append([row])
    return runs


def _select_candidates(db, watermark: int, now: float) -> tuple[list[dict], int]:
    """Scan the next batch past watermark. Returns (classify candidates,
    junk_ceiling) where junk_ceiling is the highest id it is safe to advance
    the watermark to even if zero candidates were found — the leading run of
    permanently-ineligible (junk) rows, which would otherwise starve every
    later real message behind them (2026-07-22 Codex adversarial review: a
    junk-filled batch window must not wedge the sweep forever). Settle-window
    and live-tail exclusions are NOT included in the ceiling — those are
    transient and must be reconsidered on a later tick."""
    with db._connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, thread_id, content, created_at FROM messages "
                "WHERE id > ? AND role = 'user' ORDER BY id ASC LIMIT ?",
                (watermark, BATCH_CAP),
            ).fetchall()
        ]
        max_row = conn.execute("SELECT MAX(id) AS m FROM messages").fetchone()
    max_msg_id = max_row["m"] if max_row and max_row["m"] is not None else 0
    current_thread = db.get_current_thread()

    selected: list[dict] = []
    junk_ceiling = watermark
    for row in rows:
        if _is_junk_message(row["content"]):
            if not selected:
                junk_ceiling = row["id"]
            continue
        age = _age_seconds(row["created_at"], now)
        if age is None or age < SETTLE_WINDOW_S:
            break  # settle window: sync router / orchestrator owns the tail
        if row["id"] == max_msg_id and row["thread_id"] == current_thread:
            break  # never move the live cursor's message
        selected.append(row)
    return selected, junk_ceiling


def _handle_llm_failure(db, run: list[dict]) -> None:
    """On total LLM failure: hold the watermark (retry next tick) unless the
    backoff cap is exceeded, in which case skip past the wedging run."""
    fail_count = int(db.get_setting(_FAIL_KEY) or 0) + 1
    if fail_count > FAILURE_BACKOFF_CAP:
        _log.warning(
            "Reclassify: LLM failed %d times in a row — skipping past message id %d",
            fail_count,
            run[-1]["id"],
        )
        db.set_setting(_WATERMARK_KEY, str(max(m["id"] for m in run)))
        db.set_setting(_FAIL_KEY, "0")
    else:
        db.set_setting(_FAIL_KEY, str(fail_count))


def _maybe_advance_current_thread(db, message_ids: list[int], dest: str, current_thread_at_start: str | None) -> None:
    """CAS-guarded current_thread advance — default OFF (spec Q4 open fork).
    Only ever advances when the flag is explicitly on, the moved run includes
    the genuine conversation tail, and no sync/orchestrator switch happened
    mid-batch."""
    if db.get_setting(_ADVANCE_FLAG_KEY) != "1":
        return
    with db._connect() as conn:
        row = conn.execute("SELECT MAX(id) AS m FROM messages").fetchone()
    global_max = row["m"] if row and row["m"] is not None else 0
    if global_max not in message_ids:
        return
    if db.get_current_thread() != current_thread_at_start:
        return  # CAS: someone else moved the cursor mid-batch — abort
    db.set_current_thread(dest)


def _apply_decision(
    db,
    run: list[dict],
    verdict: dict,
    open_ids: set[str],
    session_id: str,
    current_thread_at_start: str | None,
) -> None:
    decision = verdict["decision"]
    message_ids = [m["id"] for m in run]
    src_thread_id = run[0]["thread_id"]

    if decision == "move":
        dest = verdict["thread_id"]
        if (
            verdict["confidence"] >= MOVE_CONFIDENCE
            and dest
            and dest in open_ids
            and dest != src_thread_id
        ):
            db.move_messages(message_ids, dest)
            _maybe_advance_current_thread(db, message_ids, dest, current_thread_at_start)
        return  # below gate / bad target -> fail-safe stay (no-op)

    if decision == "new":
        combined = " ".join(m["content"] for m in run)
        if (
            verdict["confidence"] >= CREATE_CONFIDENCE
            and is_auto_topic_eligible(combined)
            and not _is_junk_message(combined)
            and len(combined.split()) >= MIN_WORDS_FOR_NEW_TOPIC
        ):
            topic = verdict.get("topic_hint") or combined[:60]
            new_id = db.create_thread(topic, session_id=session_id)
            db.move_messages(message_ids, new_id)
            _maybe_advance_current_thread(db, message_ids, new_id, current_thread_at_start)
        return

    # "stay" — leave messages put; nothing to do.


def run_reclassify_sweep(db, *, session_id: str = "", now: float | None = None, llm_fn=None) -> None:
    """Run one cadence-gated reclassify pass. Independently guarded by the
    caller (juggle_watchdog_daemon) so a bug here never downs the tick."""
    if now is None:
        now = time.time()

    last_run = float(db.get_setting(_AT_KEY) or 0)
    if now - last_run < RECLASSIFY_INTERVAL_S:
        return
    db.set_setting(_AT_KEY, str(now))

    if llm_fn is None:
        from llm_calls import llm_call

        def llm_fn(prompt: str) -> str | None:
            return llm_call(
                prompt, profile="cheap", timeout=20, max_tokens=150,
                json_mode=True, disable_reasoning=True,
            )

    watermark = int(db.get_setting(_WATERMARK_KEY) or 0)
    candidates, junk_ceiling = _select_candidates(db, watermark, now)
    if not candidates:
        if junk_ceiling > watermark:
            db.set_setting(_WATERMARK_KEY, str(junk_ceiling))
        return
    watermark = max(watermark, junk_ceiling)

    runs = _group_into_runs(candidates)
    open_candidates = get_classification_candidates(db.get_all_threads())
    open_ids = {c["id"] for c in open_candidates}
    current_thread_at_start = db.get_current_thread()

    for run in runs:
        prompt = build_classify_prompt(run, open_candidates, db)
        try:
            response = llm_fn(prompt)
        except Exception:
            response = None

        if response is None:
            _handle_llm_failure(db, run)
            return  # stop this tick — conservative, preserves ordering

        db.set_setting(_FAIL_KEY, "0")
        verdict = parse_classify_response(response)
        try:
            _apply_decision(db, run, verdict, open_ids, session_id, current_thread_at_start)
        except Exception:
            # Fail-safe toward "leave messages put" (spec Q5): a DB-level
            # failure applying a real verdict (e.g. MAX_THREADS cap on create)
            # must not crash the tick or wedge the watermark forever the way
            # an LLM failure would — this run got a decision, just couldn't
            # act on it.
            _log.warning(
                "Reclassify: applying decision failed for run ending id %d — leaving in place",
                run[-1]["id"],
            )
        watermark = max(watermark, max(m["id"] for m in run))
        db.set_setting(_WATERMARK_KEY, str(watermark))
