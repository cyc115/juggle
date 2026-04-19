#!/usr/bin/env python3
"""Juggle Context - builds additionalContext for UserPromptSubmit hook."""

import re

from juggle_cli_common import _humanize_dt, _extract_decision_prompt, _last_sentences
from juggle_db import JuggleDB, _is_junk_message, _thread_age_seconds
from juggle_settings import get_settings as _get_settings

# Hard cap derived from settings: ~2000 tokens => ~8000 chars
_CHAR_LIMIT: int = _get_settings()["context_injection_char_limit"]

# Max chars for a non-current thread's summary teaser
_TEASER_CHARS: int = _get_settings()["context_teaser_chars"]

# --------------------------------------------------------------------------
# Tier-based renderer helpers
# --------------------------------------------------------------------------

_STATE_EMOJI = {
    "active":   "🟢",
    "running":  "🏃",
    "closed":   "✅",
    "archived": "🗄",
}

_ARTICLE_RE = re.compile(r"\b(a|an|the)\b ", flags=re.IGNORECASE)


def _strip_articles(text: str) -> str:
    if not text:
        return ""
    return _ARTICLE_RE.sub("", text).strip()


def _minute_ts(ts: str | None) -> str:
    """Normalise a timestamp to YYYY-MM-DD HH:MM (strip seconds)."""
    if not ts:
        return ""
    s = ts.replace("T", " ").replace("Z", "")
    m = re.match(r"(\d{4}-\d{2}-\d{2}[ ]\d{2}:\d{2})", s)
    return m.group(1) if m else ""


def _current_session_id(db) -> str:
    with db._connect() as conn:
        row = conn.execute("SELECT value FROM session WHERE key = 'session_id'").fetchone()
    return row["value"] if row else ""


def _render_tier1(t: dict, db) -> list[str]:
    """Full-detail Tier 1 block (active, running)."""
    import json as _json
    label = t.get("user_label") or t.get("label") or t["id"][:6]
    state = t.get("status") or "active"
    emoji = _STATE_EMOJI.get(state, "🟢")
    title = t.get("title") or t.get("topic") or "(untitled)"
    lines = [f"[{label}] {emoji} {state} | {_strip_articles(title)}"]

    summary = _strip_articles((t.get("summary") or "").strip())
    if summary:
        lines.append(f"Summary: {summary}")

    # Open questions
    oq_raw = t.get("open_questions") or "[]"
    try:
        oq = _json.loads(oq_raw) if isinstance(oq_raw, str) else (oq_raw or [])
    except (_json.JSONDecodeError, ValueError):
        oq = []
    if oq:
        lines.append("Open questions:")
        for q in oq:
            text = q.get("text") if isinstance(q, dict) else str(q)
            lines.append(f"  - {_strip_articles(text)}")
    else:
        lines.append("Open questions: None.")

    # Q&A history — last 2 non-junk exchanges
    try:
        exchanges = db.get_recent_exchanges(t["id"], n=2)
        exchanges = [e for e in exchanges if e.get("user") or e.get("assistant")]
        if exchanges:
            lines.append("Q&A history:")
            for ex in exchanges:
                u = _strip_articles((ex.get("user") or "").strip().split("\n")[0])[:120]
                a = _strip_articles((ex.get("assistant") or "").strip().split("\n")[0])[:120]
                if u or a:
                    lines.append(f"  - Q: {u} A: {a}")
    except Exception:
        pass

    # Key decisions
    kd_raw = t.get("key_decisions") or "[]"
    try:
        kd = _json.loads(kd_raw) if isinstance(kd_raw, str) else (kd_raw or [])
    except (_json.JSONDecodeError, ValueError):
        kd = []
    if kd:
        lines.append("Key decisions:")
        for d in kd:
            # Trim seconds off any HH:MM:SS leading timestamp
            d_clean = re.sub(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2}", r"\1", str(d))
            lines.append(f"  - {_strip_articles(d_clean)}")

    return lines


def _render_tier2(t: dict) -> str:
    label = t.get("user_label") or t.get("label") or t["id"][:6]
    title = _strip_articles(t.get("title") or t.get("topic") or "")
    return f"[{label}] ✅ closed  | {title}"


def _build(db: JuggleDB) -> str:
    if not db.is_active():
        return ""

    from datetime import datetime, timezone, timedelta

    parts: list[str] = []
    parts.append("--- JUGGLE ACTIVE (do not forward to sub-agents) ---")
    parts.append("RULE: Every Agent call MUST use run_in_background=true. No foreground agents ever.")

    session_id = _current_session_id(db)

    # --- Action items (always injected while open) ---
    action_items = db.get_open_action_items()
    if action_items:
        parts.append("")
        parts.append("# Action Items")
        for it in action_items:
            thread_suffix = ""
            if it.get("thread_id"):
                t = db.get_thread(it["thread_id"])
                if t:
                    lbl = t.get("user_label") or t.get("label") or it["thread_id"][:6]
                    thread_suffix = f" (thread: [{lbl}])"
            pri = (it.get("priority") or "normal").upper()
            parts.append(f"⚡ [{it['id']}] {pri:6} {_strip_articles(it['message'])}{thread_suffix}")

    # --- Threads: Tier 1 (active + running) ---
    tier1 = [
        t for t in db.get_threads_by_status("active")
        if t.get("show_in_list", 1) != 0
    ] + [
        t for t in db.get_threads_by_status("running")
        if t.get("show_in_list", 1) != 0
    ]
    if tier1:
        parts.append("")
        parts.append("# Active Threads")
        for t in tier1:
            parts.extend(_render_tier1(t, db))
            parts.append("")

    # --- Threads: Tier 2 (closed within TTL) ---
    ttl_secs = int(db.get_setting("thread_auto_archive_ttl_secs", default="3600") or "3600")
    closed = db.get_threads_by_status("closed")
    tier2 = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_secs)
    for t in closed:
        la = t.get("last_active_at") or t.get("last_active") or ""
        try:
            dt = datetime.strptime(la[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt >= cutoff:
            tier2.append(t)
    if tier2:
        parts.append("# Closed (within TTL)")
        for t in tier2:
            parts.append(_render_tier2(t))
        parts.append("")

    # --- Notifications (current session only) ---
    notifs = db.get_notifications_for_session(session_id)
    if notifs:
        parts.append("# Notifications (this session)")
        for n in notifs:
            parts.append(f"✓ {_strip_articles(n['message'])}")
        parts.append("")

    parts.append("--- END JUGGLE ---")
    out = "\n".join(parts)

    if len(out) > _CHAR_LIMIT:
        out = _trim_to_limit(out, _CHAR_LIMIT)
    return out


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
    if assistant_row and age is not None and age > _get_settings()["thread_idle_threshold_secs"]:
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
        label = t.get("user_label") or t.get("label") or tid[:8]
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
        if summary:
            output_lines.append(f"{vert}├── Summary: {summary}")

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


        if not is_last:
            output_lines.append("│")


    return "\n".join(output_lines)


def _auto_archive_closed_threads(db: JuggleDB) -> int:
    """Archive any closed thread whose last_active_at exceeds the TTL.

    Returns count of threads archived.
    """
    from datetime import datetime, timezone, timedelta
    ttl_secs = int(db.get_setting("thread_auto_archive_ttl_secs", default="3600") or "3600")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_secs)
    archived = 0
    for t in db.get_threads_by_status("closed"):
        la = t.get("last_active_at") or t.get("last_active") or ""
        try:
            dt = datetime.strptime(la[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < cutoff:
            db.archive_thread(t["id"])  # preserves user_label
            archived += 1
    return archived


def build_startup_output(db: JuggleDB) -> str:
    """Full enriched startup string: topics tree + per-thread Hindsight recalls.

    Called by cmd_start (when threads exist) and handle_session_start (resume/compact).
    """
    # Lazy import to avoid circular dependency (juggle_context ← juggle_cmd_threads)
    from juggle_cmd_threads import _cleanup_orphaned_threads
    _cleanup_orphaned_threads(db)
    _auto_archive_closed_threads(db)

    threads = db.get_all_threads()
    active = [
        t for t in threads
        if t.get("status") != "archived" and t.get("show_in_list", 1) != 0
    ]

    if not active:
        return "Juggle active. No open topics."

    n = len(active)
    ver = _get_juggle_version()
    header = f"Juggle v{ver} active. Resuming {n} open topic{'s' if n != 1 else ''}:\n"
    return header + render_topics_tree(db)


if __name__ == "__main__":
    # Quick smoke-test / manual inspection
    import sys
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    print(build_context_string(db_path=db_path))
