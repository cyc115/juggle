#!/usr/bin/env python3
"""Juggle Context - builds additionalContext for UserPromptSubmit hook."""

import os
import re

from juggle_cli_common import _humanize_dt, _extract_decision_prompt
from juggle_db import JuggleDB, _is_junk_message, _thread_age_seconds
from juggle_settings import get_settings as _get_settings

from juggle_context_startup import (  # noqa: F401 — re-exported public API
    _auto_archive_closed_threads,
    _get_juggle_version,
    _recall_for_thread,
    build_startup_output,
    get_thread_state,
    render_topics_tree,
)

# Hard cap derived from settings: ~2000 tokens => ~8000 chars
_CHAR_LIMIT: int = _get_settings()["context_injection_char_limit"]

# Max chars for a non-current thread's summary teaser
_TEASER_CHARS: int = _get_settings()["context_teaser_chars"]

# --------------------------------------------------------------------------
# Tier-based renderer helpers
# --------------------------------------------------------------------------

_STATE_EMOJI = {
    "active": "🟢",
    "running": "🏃",
    "closed": "✅",
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
        row = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    return row["value"] if row else ""


def _graph_node_tag(db, thread_id: str) -> str:
    """`[ready]` / `[blocked:dep1,dep2]` tag for a graph-node-bound thread.

    Empty string for unbound threads or pre-migration DBs without graph tables.
    """
    try:
        from dbops import db_graph

        node = db_graph.get_node_by_thread(db, thread_id)
        if not node:
            return ""
        if node["state"] == "ready":
            return " [ready]"
        if node["state"] in ("pending", "blocked-failed"):
            deps = db_graph.unverified_deps(db, node["id"])
            if deps:
                return f" [blocked:{','.join(deps)}]"
        return ""
    except Exception:
        return ""


def _render_tier1(t: dict, db) -> list[str]:
    """Full-detail Tier 1 block (active, running)."""
    import json as _json

    label = t.get("user_label") or t.get("label") or t["id"][:6]
    state = t.get("status") or "active"
    emoji = _STATE_EMOJI.get(state, "🟢")
    title = t.get("title") or t.get("topic") or "(untitled)"
    node_tag = _graph_node_tag(db, t["id"])
    lines = [f"[{label}] {emoji} {state} | {_strip_articles(title)}{node_tag}"]

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
                a = _strip_articles((ex.get("assistant") or "").strip().split("\n")[0])[
                    :120
                ]
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


def render_agent_role_anchor_for(role: str) -> str:
    """Return the AGENT ROLE anchor block for an explicit ``role``.

    Shared by the env-driven hook path (``_render_agent_role_anchor``, used by
    Claude Code's UserPromptSubmit hook) and by harness adapters that inline the
    anchor into the task prompt for harnesses without juggle hooks
    (``juggle_harness.HarnessAdapter.decorate_task``). Returns "" for an unknown
    or unconfigured role.
    """
    if not role:
        return ""
    role_context = _get_settings().get("agent", {}).get("role_context", {})
    identity = role_context.get(role, "")
    if not identity:
        return ""
    from pathlib import Path as _Path

    plugin_root = os.environ.get(
        "CLAUDE_PLUGIN_ROOT", str(_Path(__file__).resolve().parent.parent)
    )
    return (
        "--- AGENT ROLE ---\n"
        f"ROLE: {role}. {identity}\n"
        f'COMPLETION: python3 {plugin_root}/src/juggle_cli.py complete-agent <THREAD> "<summary>" --retain "<key finding>"'
    )


def _render_agent_role_anchor() -> str:
    """Return the AGENT ROLE anchor block for agent panes. Empty string otherwise."""
    if os.environ.get("JUGGLE_IS_AGENT") != "1":
        return ""
    return render_agent_role_anchor_for(os.environ.get("JUGGLE_AGENT_ROLE", ""))


def _build(db: JuggleDB) -> str:
    # Agent sessions get ONLY their role anchor — never the orchestrator
    # dashboard. The "JUGGLE ACTIVE" block is orchestrator-only context
    # (explicitly tagged "do not forward to sub-agents") and an agent is told
    # to ignore all of it, so injecting it wastes up to context_injection_char_limit
    # (~2000 tokens) per task prompt on every dispatched agent. Returned before
    # any DB query so it costs nothing and needs no active orchestrator.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        return _render_agent_role_anchor()

    if not db.is_active():
        return ""

    from datetime import datetime, timezone, timedelta

    parts: list[str] = []
    parts.append("--- JUGGLE ACTIVE (do not forward to sub-agents) ---")
    parts.append(
        "RULE: Every Agent call MUST use run_in_background=true. No foreground agents ever."
    )

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
            parts.append(
                f"⚡ [{it['id']}] {pri:6} {_strip_articles(it['message'])}{thread_suffix}"
            )

    # --- Threads: Tier 1 (active + running) ---
    tier1 = [
        t for t in db.get_threads_by_status("active") if t.get("show_in_list", 1) != 0
    ] + [
        t for t in db.get_threads_by_status("running") if t.get("show_in_list", 1) != 0
    ]
    if tier1:
        parts.append("")
        parts.append("# Active Threads")
        for t in tier1:
            parts.extend(_render_tier1(t, db))
            parts.append("")

    # --- Threads: Tier 2 (closed within TTL) ---
    ttl_secs = int(
        db.get_setting("thread_auto_archive_ttl_secs", default="3600") or "3600"
    )
    closed = db.get_threads_by_status("closed")
    tier2 = []
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_secs)
    for t in closed:
        la = t.get("last_active_at") or t.get("last_active") or ""
        try:
            dt = datetime.strptime(la[:16], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if dt >= cutoff:
            tier2.append(t)
    if tier2:
        parts.append("# Closed (within TTL)")
        for t in tier2:
            parts.append(_render_tier2(t))
        parts.append("")

    # --- Notifications with per-Claude-session watermark (ID-based) ---
    import os as _os
    claude_sess = _os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    last_id = db.get_notif_watermark(claude_sess) if claude_sess else None
    if last_id is None:
        notifs = db.get_notifications_last_n(session_id, n=5)
    else:
        notifs = db.get_notifications_since_id(session_id, last_id)
    if claude_sess:
        new_last = max((n["id"] for n in notifs), default=last_id or 0)
        db.set_notif_watermark(claude_sess, new_last)
    if notifs:
        parts.append("# Notifications (this session)")
        for n in notifs:
            parts.append(f"✓ {_strip_articles(n['message'])}")
        parts.append("")

    parts.append("--- END JUGGLE ---")
    out = "\n".join(parts)

    if len(out) > _CHAR_LIMIT:
        out = _trim_to_limit(out, _CHAR_LIMIT)

    # Inject role anchor after trim so it is never cut by the char limit.
    role_anchor = _render_agent_role_anchor()
    if role_anchor:
        footer = "--- END JUGGLE ---"
        if out.endswith(footer):
            out = out[: -len(footer)] + role_anchor + "\n" + footer
        else:
            out += "\n" + role_anchor

    return out


def _trim_to_limit(text: str, limit: int) -> str:
    """
    Trim text to at most `limit` chars while preserving the header/footer lines
    and as much of the body as possible.
    """
    lines = text.splitlines()
    if not lines:
        return text

    header = lines[0]  # "--- JUGGLE ACTIVE (do not forward to sub-agents) ---"
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
    cut = body[: body_budget - 4]
    cut = cut[: cut.rfind(" ")] if " " in cut else cut
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
