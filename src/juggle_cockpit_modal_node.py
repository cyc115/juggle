"""Juggle Cockpit — unified node-detail modal.

Extracted from juggle_cockpit_modals.py (LOC gate). Holds the single modal that
renders BOTH topic and task graph nodes, plus the session-scoped summary cache.
Re-exported from juggle_cockpit_modals for backward compatibility
(``from juggle_cockpit_modals import _NodeDetailModal``). The shared
``build_summary_ctx`` helper lives in juggle_cockpit_modals alongside the other
resolver helpers.
"""

from __future__ import annotations

import logging

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

_log = logging.getLogger(__name__)

# Session-scoped summary cache: (thread_id, message_count) → {context,why,what,result}
_topic_summary_cache: dict[tuple, dict] = {}


class _NodeDetailModal(ModalScreen):
    """Unified read-only detail overlay for a graph node — topic OR task.

    TOPIC nodes render: header ``Topic [<label>] - <title>``, the structured
    fields (state / deps / thread / verify), the member-``tasks:`` list, an
    LLM-generated ``Summary:`` (Context / Why / What / Result) loaded ASYNC in a
    background thread, and a ``Recent Activity:`` tail. The header renders
    immediately; the summary streams in below (cache-keyed by
    ``(thread_id, message_count)`` — re-opens are instant).

    TASK nodes (kind='task') render the header ``Task <id>`` plus the structured
    fields and prompt/handoff excerpts ONLY — no Summary / Recent Activity.

    Opened from BOTH the graph-panel Enter key and the topic-list 'i' key.
    Dismisses on 'q' or Escape.
    """

    from textual.binding import Binding

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    _NodeDetailModal {
        align: center middle;
    }
    _NodeDetailModal > VerticalScroll {
        width: 70%;
        height: 70%;
        border: round $accent;
        padding: 1 2;
    }
    """

    _EXCERPT = 400

    def __init__(
        self,
        node: dict,
        deps: list[str],
        *,
        is_topic: bool,
        tasks: list | None = None,
        summary_ctx: dict | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__()
        # NB: store as _node, NOT _task — textual's MessagePump uses ``self._task``
        # for its message-loop asyncio Task and would clobber it.
        self._node = node
        self._deps = deps or []
        self._is_topic = is_topic
        self._tasks = tasks or []
        self._summary_ctx = summary_ctx or {}
        self._label = label or node.get("id", "?")
        self._cursor = 0  # MAX(messages.id) cursor; resolved in on_mount

    # -- adapters ----------------------------------------------------------

    @classmethod
    def from_conversation(cls, topic, extra: dict | None = None) -> "_NodeDetailModal":
        """Build a TOPIC modal from a cockpit-model Topic (conversation thread).

        Used by the 'i' key path, where the resolved object is a conversation
        thread (``label`` / ``title`` / ``status`` / ``task_state``) rather than
        a graph nodes row.
        """
        extra = extra or {}
        node = {
            "id": topic.label,
            "title": topic.title or "",
            "state": topic.status,
            "thread_id": extra.get("thread_id") or getattr(topic, "id", None),
            "verify_cmd": None,
            "task_state": getattr(topic, "task_state", None),
        }
        return cls(node, [], is_topic=True, summary_ctx=extra, label=topic.label)

    # -- header / structured fields (render immediately) -------------------

    def _field_lines(self) -> list[str]:
        from juggle_cockpit_view import TASK_STATE_GLYPHS

        n = self._node
        if self._is_topic:
            title = n.get("title") or "(none)"
            out = [
                f"Topic [{self._label}] - {title}",
                "─" * 40,
                f"state    {n.get('state', '')}",
                f"deps     {', '.join(self._deps) if self._deps else '(none)'}",
                f"thread   {n.get('thread_id') or '(unbound)'}",
                f"verify   {n.get('verify_cmd') or '(none)'}",
            ]
            if n.get("task_state"):
                out.append(f"task     {n.get('task_state')}")
            agent = self._summary_ctx.get("agent")
            if agent:
                out.append(f"agent    {agent}")
        else:
            out = [
                f"Task {n.get('id', '?')}",
                "─" * 40,
                f"title    {n.get('title', '')}",
                f"state    {n.get('state', '')}",
                f"deps     {', '.join(self._deps) if self._deps else '(none)'}",
                f"thread   {n.get('thread_id') or '(unbound)'}",
                f"verify   {n.get('verify_cmd') or '(none)'}",
            ]
        if self._tasks:
            out += ["", "tasks:"]
            for t in self._tasks:
                glyph = TASK_STATE_GLYPHS.get(t.get("state", ""), "⬢")
                out.append(f"  {glyph} {t.get('id', '')}  {t.get('title', '')}")
        return out

    def _task_extra_lines(self) -> list[str]:
        """prompt / handoff excerpts — task nodes only."""
        n = self._node
        out: list[str] = []
        prompt = (n.get("prompt") or "").strip()
        if prompt:
            out += ["", "prompt:", prompt[: self._EXCERPT]]
        handoff = (n.get("handoff") or "").strip()
        if handoff:
            out += ["", "handoff:", handoff[: self._EXCERPT]]
        return out

    def _raw_body_lines(self) -> list[str]:
        """Fallback body when an LLM summary is unavailable (topic nodes)."""
        out: list[str] = []
        summary = (self._summary_ctx.get("summary") or "").strip()
        if summary:
            out += ["", "summary:", summary]
        task_input = (self._summary_ctx.get("task_input") or "").strip()
        if task_input:
            out += ["", "task / input:", task_input]
        result_output = (self._summary_ctx.get("result_output") or "").strip()
        if result_output:
            out += ["", "output / result:", result_output]
        return out

    def _summary_body_lines(self, sections: dict, note: str = "") -> list[str]:
        """Render the four LLM sections + recent activity (topic nodes)."""
        from juggle_topic_summary import format_recent_activity

        out: list[str] = ["", "Summary:"]
        labels = [("Context", "context"), ("Why", "why"), ("What", "what"), ("Result", "result")]
        for display, key in labels:
            val = (sections.get(key) or "").strip()
            if val:
                out += ["", f"{display}:", val]
        if note:
            out += ["", note]
        messages_all = self._summary_ctx.get("messages_all") or self._summary_ctx.get("recent") or []
        activity = format_recent_activity(messages_all, limit=5)
        if activity:
            out += ["", "Recent Activity:"]
            for line in activity:
                out.append(f"- {line}")
        return out

    def _lines(self) -> list[str]:
        """Combined header + raw fallback body. Sync helper for tests."""
        out = self._field_lines()
        if self._is_topic:
            out += self._raw_body_lines()
            recent = self._summary_ctx.get("recent") or []
            if recent:
                out += ["", "recent activity:"]
                for msg in recent:
                    role = msg.get("role", "?")
                    content = (msg.get("content") or "").strip()
                    out.append(f"[{role}] {content}")
            elif self._summary_ctx.get("recent_msg"):
                out += ["", "recent:", (self._summary_ctx["recent_msg"] or "").strip()]
        else:
            out += self._task_extra_lines()
        out += ["", "Esc / q — close"]
        return out

    # -- compose / async summary -------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("\n".join(self._field_lines()), id="node-header", markup=False)
            yield Static("", id="node-body", markup=False)

    def on_mount(self) -> None:
        # Task nodes have no summary — render prompt/handoff + close hint and stop.
        if not self._is_topic:
            self.query_one("#node-body", Static).update(
                "\n".join(self._task_extra_lines() + ["", "Esc / q — close"])
            )
            return

        # No conversation to summarise (unbound topic) — show raw fallback only.
        if not self._summary_ctx.get("messages_all"):
            self._apply_summary({})
            return

        from juggle_topic_summary_cache import load_cached_sections

        thread_id = self._summary_ctx.get("thread_id", "")
        message_count = self._summary_ctx.get("message_count", 0)
        db = getattr(self.app, "_db", None)

        # L1 (in-memory) → L2 (DB) lookup keyed by MAX(messages.id). An EXACT hit
        # renders instantly; a miss / advanced cursor regenerates.
        sections, self._cursor = load_cached_sections(
            db, thread_id, message_count, _topic_summary_cache
        )
        if sections is not None:
            self._apply_summary(sections)
            return

        self.query_one("#node-body", Static).update("Summarizing…")
        self.run_worker(self._fetch_summary, thread=True)

    def _fetch_summary(self) -> None:
        """Blocking worker: call LLM, persist a usable summary, update body."""
        from juggle_topic_summary import summarize_topic
        from juggle_topic_summary_cache import store_summary

        task_input = (self._summary_ctx.get("task_input") or "").strip()
        result_output = (self._summary_ctx.get("result_output") or "").strip()
        messages_all = self._summary_ctx.get("messages_all") or self._summary_ctx.get("recent") or []
        meta = {
            "label": self._label,
            "title": self._node.get("title") or "",
            "status": self._node.get("state") or "",
        }

        sections = summarize_topic(task_input, result_output, messages_all, meta)

        # R7: persist ONLY a displayable summary (store_summary gates on content);
        # an empty / LLM-failed one is never cached, so the next view re-derives.
        thread_id = self._summary_ctx.get("thread_id", "")
        cursor = getattr(self, "_cursor", self._summary_ctx.get("message_count", 0))
        db = getattr(self.app, "_db", None)
        store_summary(db, thread_id, cursor, sections, _topic_summary_cache)

        self.app.call_from_thread(self._apply_summary, sections)

    def _apply_summary(self, sections: dict) -> None:
        """Update body widget with summarised or fallback content (UI thread)."""
        any_content = any((sections.get(k) or "").strip() for k in ("context", "why", "what", "result"))

        if any_content:
            _log.info("_apply_summary: branch=summary (sections filled)")
            body_lines = self._summary_body_lines(sections)
        else:
            _log.warning("_apply_summary: branch=raw_fallback (0 sections filled — check summarize logs)")
            body_lines = self._raw_body_lines() + ["", "(summary unavailable)"]

        body_lines += ["", "Esc / q — close"]
        self.query_one("#node-body", Static).update("\n".join(body_lines))
