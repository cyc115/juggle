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

# Unified node-detail modal extracted to its own module (LOC gate); re-exported
# here so ``from juggle_cockpit_modals import _NodeDetailModal`` keeps working,
# along with the session summary cache.
from juggle_cockpit_modal_node import (  # noqa: E402,F401
    _NodeDetailModal,
    _topic_summary_cache,
)


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
                "action": "graph_railroad",
                "key": "G",
                "short": "Rail",
                "desc": "Open the full-screen dependency railroad for the selected task's project",
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
    from juggle_cockpit_legend import render_legend_lines
    lines += render_legend_lines()
    lines += ["", "Esc / q — close   ·   ↑ ↓ / j k — scroll"]
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
    _HelpModal > VerticalScroll {
        width: 76;
        max-width: 90%;
        height: 90%;
        border: round $accent;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("\n".join(render_help_lines()))

    def on_key(self, event: events.Key) -> None:
        if event.key in ("j", "down"):
            self.query_one(VerticalScroll).scroll_down()
            event.stop()
        elif event.key in ("k", "up"):
            self.query_one(VerticalScroll).scroll_up()
            event.stop()


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
    # Child task-nodes drive the Sub-tasks prompt block AND the node-aware cache
    # fingerprint (updated_at feeds child_node_signature). Isolated try so a
    # missing nodes table never costs the message context above.
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            child_rows = conn.execute(
                "SELECT id, title, state, agent_result, updated_at FROM nodes "
                "WHERE parent_id = ? AND kind = 'task' ORDER BY id ASC",
                (thread_id,),
            ).fetchall()
        ctx["child_nodes"] = [
            {
                "id": r["id"],
                "title": r["title"],
                "state": r["state"],
                "agent_result": r["agent_result"],
                "updated_at": r["updated_at"],
            }
            for r in child_rows
        ]
    except Exception:
        pass
    return ctx


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
    """Project arm/disarm overlay (p key) — default-armed exclusion model.

    Every active project is armed unless explicitly DISARMED; the stored
    authority is the disarmed exclusion set. j/k navigate, Space/Enter toggle the
    cursor row (armed→disarm, disarmed→arm — NOT the global flag), A arm-all
    (clear the set), D disarm-all (exclude every project), Esc/q close. Row
    display: ● armed / ○ disarmed, X/Y progress, running count, (complete) /
    — no graph hint. The header shows the GLOBAL ON/OFF flag for context;
    disarm choices only take effect while global autopilot is ON.
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
                "[dim]j/k navigate  Space/Enter arm·disarm  A arm-all  D disarm-all  Esc close[/dim]",
                markup=True,
            )

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        from juggle_cmd_autopilot import AUTOPILOT_FLAG
        from juggle_graph_status import graph_counts
        from juggle_autopilot_state import get_disarmed_projects

        projects = self._db.list_projects()
        counts = {p["id"]: graph_counts(self._db, p["id"]) for p in projects}
        disarmed = set(get_disarmed_projects(self._db))
        armed = {p["id"] for p in projects if p["id"] not in disarmed}
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

    def _apply_toggle(self) -> None:
        """Pure backend mutation for the cursor row (no widget access — testable):
        armed → disarm, disarmed → arm."""
        from juggle_autopilot_state import arm_project, disarm_project

        if not self._rows:
            return
        row = self._rows[self._cursor]
        if row.armed:
            disarm_project(self._db, row.pid)
        else:
            arm_project(self._db, row.pid)

    def _toggle_current(self) -> None:
        self._apply_toggle()
        self._refresh()

    def _arm_all(self) -> None:
        from juggle_autopilot_state import arm_all

        arm_all(self._db)  # clear the disarmed set — every project armed
        self._refresh()

    def _disarm_all(self) -> None:
        from juggle_autopilot_state import disarm_all

        disarm_all(self._db, [r.pid for r in self._rows])
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
        elif event.key == "D":
            self._disarm_all()
            event.stop()
