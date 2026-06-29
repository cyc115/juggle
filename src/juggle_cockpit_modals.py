"""Juggle Cockpit — Textual modal screens.

Extracted from juggle_cockpit.py for modularity.
All symbols are re-exported from juggle_cockpit for backward compatibility.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from textual import events

_log = logging.getLogger(__name__)
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static


# ---------------------------------------------------------------------------
# Project-arm row model (pure, no I/O — unit-testable without a live terminal)
# ---------------------------------------------------------------------------

@dataclass
class ProjectArmRow:
    pid: str
    name: str
    armed: bool
    verified: int
    total: int
    running: int
    hint: str  # "(complete)" | "— no graph" | ""


def build_project_arm_rows(
    projects: list[dict],
    armed_set: set[str],
    task_counts: dict[str, dict | None],
) -> list[ProjectArmRow]:
    """Pure row builder for the project-arm modal — no I/O, fully unit-testable."""
    rows = []
    for p in projects:
        pid = p["id"]
        counts = task_counts.get(pid)
        if counts:
            verified = counts.get("verified", 0)
            total = counts.get("total", 0)
            running = counts.get("running", 0)
            hint = "(complete)" if total > 0 and verified == total else ""
        else:
            verified = total = running = 0
            hint = "— no graph"
        rows.append(ProjectArmRow(
            pid=pid,
            name=p.get("name", pid),
            armed=pid in armed_set,
            verified=verified,
            total=total,
            running=running,
            hint=hint,
        ))
    return rows


class _PromptModal(ModalScreen):
    """Generic one-line input modal. Dismisses with the stripped value or None.

    dismiss_empty_as: value returned when the Input is blank (default None).
    Set to "" in action_filter so blank submit clears the filter, while Esc
    still returns None meaning "keep existing filter unchanged".
    """

    DEFAULT_CSS = """
    _PromptModal {
        align: center middle;
    }
    _PromptModal > Vertical {
        width: 44;
        height: 6;
        border: round $accent;
        padding: 1 2;
    }
    """

    def __init__(self, prompt: str, dismiss_empty_as=None) -> None:
        super().__init__()
        self._prompt = prompt
        self._dismiss_empty_as = dismiss_empty_as

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._prompt)
            yield Input(placeholder="…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        self.dismiss(val if val else self._dismiss_empty_as)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()  # prevent Esc from bubbling to CockpitApp.on_key
            if self.is_current:
                self.dismiss(None)


class _ConfirmModal(ModalScreen):
    """Single-keypress y/N confirm gate.

    Dismisses True on 'y', False on 'n' or Escape. No Input widget —
    the user only presses a single key. Cannot be submitted accidentally.
    """

    DEFAULT_CSS = """
    _ConfirmModal {
        align: center middle;
    }
    _ConfirmModal > Vertical {
        width: 52;
        height: 6;
        border: round $warning;
        padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            yield Label("[dim]y — confirm    n / Esc — cancel[/dim]")

    def on_key(self, event: events.Key) -> None:
        if not self.is_current:
            return
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)


# ---------------------------------------------------------------------------
# Structured help table — single source of truth for the ? modal.
# 'short' matches Binding description (terse, for footer).
# 'desc'  is the full human-readable explanation shown in the modal.
# ---------------------------------------------------------------------------

COCKPIT_HELP_TABLE: list[dict] = [
    {
        "group": "Navigation",
        "entries": [
            {
                "action": "scroll_down",
                "key": "j / k  (↓ / ↑)",
                "short": "↓↑",
                "desc": "Scroll active pane down / up one row",
            },
            {
                "action": "page_down",
                "key": "PgDn / PgUp",
                "short": "PgDn/Up",
                "desc": "Scroll active pane down / up 5 rows",
            },
            {
                "action": "cycle_pane",
                "key": "Tab",
                "short": "Tab",
                "desc": "Cycle active pane (topics → actions → agents → …)",
            },
            {
                "action": "focus_pane",
                "key": "f",
                "short": "Foc",
                "desc": "Focus tmux pane of agent by 1-based index",
            },
        ],
    },
    {
        "group": "Thread actions",
        "entries": [
            {
                "action": "switch",
                "key": "s",
                "short": "Sw",
                "desc": "Switch active thread by label (prompts for label)",
            },
            {
                "action": "ack",
                "key": "a",
                "short": "Ack",
                "desc": "Ack all open action items on a thread (Z = orphaned)",
            },
            {
                "action": "close",
                "key": "C  (Shift+C)",
                "short": "Cl",
                "desc": "Close thread by label (requires y/N confirm)",
            },
            {
                "action": "archive",
                "key": "x",
                "short": "Ar",
                "desc": "Archive thread by label (requires y/N confirm)",
            },
        ],
    },
    {
        "group": "Agent / pane",
        "entries": [
            {
                "action": "tail_toggle",
                "key": "t",
                "short": "Tl",
                "desc": "Open live tail overlay for agent's tmux pane",
            },
            {
                "action": "decommission",
                "key": "d",
                "short": "Dc",
                "desc": "Decommission agent by index (queues decommission_pending)",
            },
        ],
    },
    {
        "group": "Views & modals",
        "entries": [
            {
                "action": "task_detail",
                "key": "i",
                "short": "Info",
                "desc": "Show topic info (label, title, task input, result) by thread label",
            },
            {
                "action": "toggle_graph",
                "key": "g",
                "short": "Gr",
                "desc": "Toggle graph mode / notifications panel",
            },
            {
                "action": "projects",
                "key": "p",
                "short": "Proj",
                "desc": "Open project arm/disarm overlay",
            },
            {
                "action": "watchdog_toggle",
                "key": "w",
                "short": "Wd",
                "desc": "Toggle watchdog: stop if running, start if stopped",
            },
            {
                "action": "watchdog_restart",
                "key": "r  (Shift+W)",
                "short": "Rwd",
                "desc": "Kill and relaunch watchdog from canonical main branch",
            },
            {
                "action": "filter",
                "key": "/",
                "short": "Flt",
                "desc": "Filter active pane (blank to clear; actions: priority:high)",
            },
        ],
    },
    {
        "group": "App",
        "entries": [
            {
                "action": "help",
                "key": "?",
                "short": "Help",
                "desc": "Show this keyboard shortcuts overlay",
            },
            {
                "action": "quit",
                "key": "Ctrl+C",
                "short": "Quit",
                "desc": "Quit the cockpit",
            },
        ],
    },
]

def build_help_content() -> list[dict]:
    """Return COCKPIT_HELP_TABLE with scroll_up/page_up aliases merged.

    scroll_up and page_up share display rows with scroll_down/page_down,
    so they are intentionally absent from COCKPIT_HELP_TABLE.  This function
    is the authoritative content builder for the modal — tests assert on it.
    """
    return COCKPIT_HELP_TABLE


def render_help_lines() -> list[str]:
    """Render help content as aligned text lines for the modal Static widget.

    Three-column layout: key (left-aligned) | short (fixed-width) | full desc.
    Group headers are displayed as section separators.
    """
    KEY_W = 18
    SHORT_W = 6
    lines: list[str] = ["Keyboard Shortcuts", "─" * 52]
    for group in COCKPIT_HELP_TABLE:
        lines.append("")
        lines.append(f"  {group['group']}")
        lines.append("  " + "─" * 48)
        for entry in group["entries"]:
            key = entry["key"]
            short = entry["short"]
            desc = entry["desc"]
            lines.append(f"  {key:<{KEY_W}}  {short:<{SHORT_W}}  {desc}")
    lines += ["", "Esc / q — close"]
    return lines


class _HelpModal(ModalScreen):
    """Help overlay — grouped keyboard shortcuts with full descriptions."""

    from textual.binding import Binding

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    _HelpModal {
        align: center middle;
    }
    _HelpModal > Static {
        width: 72;
        border: round $accent;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("\n".join(render_help_lines()))


def build_summary_ctx(db, thread_id: str | None) -> dict:
    """Gather LLM-summary context for a dispatch/conversation thread.

    Shared by BOTH unified-modal entry points (graph-panel Enter and the
    topic-list 'i' key) so the message-fetch logic lives in one place. Returns
    {"thread_id": ...} plus task_input / result_output / messages_all / recent /
    message_count when available. Degrades to a bare dict on any failure — the
    modal then falls back to the raw body / objective.
    """
    import sqlite3

    ctx: dict = {"thread_id": thread_id or ""}
    if db is None or not thread_id:
        return ctx
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            first_row = conn.execute(
                "SELECT content FROM messages WHERE thread_id = ? AND role = 'user' "
                "ORDER BY id ASC LIMIT 1",
                (thread_id,),
            ).fetchone()
            all_rows = conn.execute(
                "SELECT role, content FROM messages WHERE thread_id = ? ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
        if first_row:
            ctx["task_input"] = (first_row["content"] or "").strip()
        try:
            exchange = db.get_last_exchange(thread_id)
            last_asst = (exchange.get("last_assistant") or "").strip()
            if last_asst:
                ctx["result_output"] = last_asst
        except Exception:
            pass
        messages_all = [
            {"role": r["role"], "content": r["content"]} for r in all_rows
        ]
        ctx["messages_all"] = messages_all[-20:]
        ctx["recent"] = messages_all[-5:]
        ctx["message_count"] = len(messages_all)
    except Exception:
        pass
    return ctx


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


def resolve_task_detail(
    tasks: list[dict], query: str
) -> "tuple[dict, list[str]] | None":
    """Pure resolver: match query against task id, unique id prefix, or _label.

    Priority:
      1. Exact task id (case-insensitive)
      2. Unique task id prefix (case-insensitive); exact beats prefix
      3. _label field (thread user_label slug, case-insensitive)

    Returns (matched_task, deps_list) or None.
    deps_list is taken from the task's own 'deps' field.
    Ambiguous prefix (multiple matches, none exact) → None.
    """
    if not tasks or not query:
        return None

    q = query.strip().upper()

    # Priority 1: exact id
    for t in tasks:
        if (t.get("id") or "").upper() == q:
            return (t, list(t.get("deps") or []))

    # Priority 2: unique id prefix
    prefix_matches = [t for t in tasks if (t.get("id") or "").upper().startswith(q)]
    if len(prefix_matches) == 1:
        t = prefix_matches[0]
        return (t, list(t.get("deps") or []))

    # Priority 3: _label match (injected by caller from thread.user_label)
    label_matches = [t for t in tasks if (t.get("_label") or "").upper() == q]
    if len(label_matches) == 1:
        t = label_matches[0]
        return (t, list(t.get("deps") or []))

    return None


def resolve_thread_detail(topics: list, query: str):
    """Match query against topic.label (case-insensitive). Returns Topic or None."""
    if not topics or not query:
        return None
    q = query.strip().upper()
    for topic in topics:
        if (topic.label or "").upper() == q:
            return topic
    return None


# Session-scoped summary cache: (thread_id, message_count) → {context,why,what,result}
_topic_summary_cache: dict[tuple, dict] = {}


class _TailModal(ModalScreen):
    """Live tail overlay for a tmux pane.

    Pops on top of the cockpit when the user presses 't' + agent index.
    Refreshes every 1 s via set_interval; dismiss on 't' or 'escape'.
    Shows the last 100 lines in a scrollable pane (↑/↓/PgUp/PgDn) and
    follows the tail (auto-scrolls to newest) unless the user scrolls up.

    capture_fn is injected (not imported) so the modal stays testable
    without subprocess access.
    """

    TAIL_LINES = 100

    DEFAULT_CSS = """
    _TailModal {
        align: center middle;
    }
    _TailModal > Vertical {
        width: 80%;
        height: 70%;
        border: round $accent;
        padding: 1 2;
    }
    _TailModal #tail-scroll {
        height: 1fr;
    }
    """

    def __init__(self, pane_id: str, capture_fn: Callable[..., str]) -> None:
        super().__init__()
        self.pane_id = pane_id
        self._capture_fn = capture_fn

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"tail {self.pane_id}   (↑/↓/j/k/PgUp/PgDn to scroll · q / t / esc to close)",
                markup=False,
            )
            with VerticalScroll(id="tail-scroll"):
                yield Static("", id="tail-modal-body", markup=False)

    def on_mount(self) -> None:
        self.query_one("#tail-scroll", VerticalScroll).focus()
        self._refresh_tail()
        self.set_interval(1.0, self._refresh_tail)

    def _refresh_tail(self) -> None:
        scroll = self.query_one("#tail-scroll", VerticalScroll)
        # Follow the tail only when the user is already pinned to the bottom;
        # if they've scrolled up to read history, leave their position alone.
        at_bottom = scroll.scroll_offset.y >= scroll.max_scroll_y
        from rich.text import Text as RichText

        text = self._capture_fn(self.pane_id, lines=self.TAIL_LINES)
        body: str | RichText = RichText(text) if text else RichText("(no output)", style="dim")
        self.query_one("#tail-modal-body", Static).update(body)
        if at_bottom:
            scroll.scroll_end(animate=False)

    def on_key(self, event: events.Key) -> None:
        if event.key in ("q", "t", "escape"):
            event.stop()
            if self.is_current:
                self.dismiss()
        elif event.key == "j":
            self.query_one("#tail-scroll", VerticalScroll).scroll_down()
            event.stop()
        elif event.key == "k":
            self.query_one("#tail-scroll", VerticalScroll).scroll_up()
            event.stop()


class _ProjectArmModal(ModalScreen):
    """Project arm/disarm overlay (p key).

    Multi-arm: armed is a SET. j/k navigate, Space/Enter toggle, A arm-all,
    Esc/q close. Row display: ● armed / ○ disarmed, X/Y progress, running count,
    (complete) / — no graph hint.
    """

    from textual.binding import Binding

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    _ProjectArmModal { align: center middle; }
    _ProjectArmModal > Vertical {
        width: 80; height: auto; max-height: 80%;
        border: round $accent; padding: 1 2;
    }
    _ProjectArmModal #proj-list { height: auto; }
    """

    def __init__(self, db) -> None:
        super().__init__()
        self._db = db
        self._cursor = 0
        self._rows: list[ProjectArmRow] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("", id="proj-header", markup=True)
            yield Static("", id="proj-list", markup=False)
            yield Static(
                "[dim]j/k navigate  Space/Enter toggle  A arm-all  Esc close[/dim]",
                markup=True,
            )

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        from juggle_cmd_autopilot import AUTOPILOT_FLAG
        from juggle_graph_status import graph_counts

        projects = self._db.list_projects()
        counts = {p["id"]: graph_counts(self._db, p["id"]) for p in projects}
        # P7: per-project arming removed — all projects are always active
        armed = {p["id"] for p in projects}
        self._rows = build_project_arm_rows(projects, armed, counts)
        self._cursor = min(self._cursor, max(0, len(self._rows) - 1))

        global_s = "ON" if AUTOPILOT_FLAG.exists() else "OFF"
        colour = "green" if global_s == "ON" else "red"
        self.query_one("#proj-header", Static).update(
            f"[bold]Projects[/bold]  global: [{colour}]{global_s}[/{colour}]"
        )
        lines = []
        for i, r in enumerate(self._rows):
            cur = "▶ " if i == self._cursor else "  "
            dot = "●" if r.armed else "○"
            prog = f"{r.verified}/{r.total}" if r.total else "—/—"
            run_s = f"  · {r.running} running" if r.running else ""
            hint = f"  {r.hint}" if r.hint else ""
            lines.append(f"{cur}{dot}  {r.pid:<18} {r.name:<16} {prog:>6}{run_s}{hint}")
        self.query_one("#proj-list", Static).update(
            "\n".join(lines) if lines else "(no projects)"
        )

    def _toggle_current(self) -> None:
        # P7: per-project arming removed — toggle is now the global on/off flag
        from juggle_cmd_autopilot import AUTOPILOT_FLAG, _flag_set

        _flag_set(not AUTOPILOT_FLAG.exists())
        self._refresh()

    def _arm_all(self) -> None:
        # P7: per-project arming removed — "arm all" enables the global flag
        from juggle_cmd_autopilot import _flag_set

        _flag_set(True)
        self._refresh()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("j", "down"):
            self._cursor = min(self._cursor + 1, max(0, len(self._rows) - 1))
            self._refresh()
            event.stop()
        elif event.key in ("k", "up"):
            self._cursor = max(self._cursor - 1, 0)
            self._refresh()
            event.stop()
        elif event.key in ("space", "enter"):
            self._toggle_current()
            event.stop()
        elif event.key == "A":
            self._arm_all()
            event.stop()
