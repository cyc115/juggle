#!/usr/bin/env python3
"""Juggle Cockpit — three-column live terminal dashboard.

Display-only. Never writes to DB. Never calls subprocess.

Run:  python3 src/juggle_cockpit.py
Exit: Ctrl-C
"""

import json
import re
import shutil
import signal
import sys
import time

from juggle_db import JuggleDB
from juggle_db import _thread_age_seconds  # private import — acceptable for v1
from juggle_context import get_thread_state
from juggle_settings import get_settings as _get_settings

_last_reap_time = 0


# ---------------------------------------------------------------------------
# Rich-based tick — model/view layer (Tasks 13-14)
# ---------------------------------------------------------------------------

def tick(db, size, prev_layout, prev_bp):
    """One cockpit tick: snapshot DB → pick breakpoint → render into layout.

    Returns (layout, bp). Reuses prev_layout when breakpoint is unchanged.
    """
    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_cockpit_view import pick_breakpoint as _pick_bp, build_layout as _build_layout, render_into as _render_into

    bp = _pick_bp(size)
    if prev_layout is None or prev_bp != bp:
        layout = _build_layout(bp)
    else:
        layout = prev_layout

    state = _snapshot(db)
    _render_into(layout, state, bp)
    return layout, bp


def _throttled_reaper(db, mgr, throttle_secs=60):
    """Reap agents, throttled to once per throttle_secs."""
    global _last_reap_time
    now = time.time()
    if now - _last_reap_time >= throttle_secs:
        from juggle_tmux import reap_stale_agents
        reap_stale_agents(db, mgr)
        _last_reap_time = now

# ---------------------------------------------------------------------------
# ANSI constants
# ---------------------------------------------------------------------------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
WHITE = "\033[97m"

REFRESH_INTERVAL: float = _get_settings()["cockpit"]["refresh_interval_secs"]

# Priority tiers (lower = higher priority)
TIER_BLOCKER = 0
TIER_REVIEW = 1
TIER_ACTIVE = 2
TIER_CURRENT = 3
TIER_WAITING = 4
TIER_IDLE = 5
TIER_DONE = 6


# ---------------------------------------------------------------------------
# String / display helpers
# ---------------------------------------------------------------------------

def strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from s."""
    return re.sub(r"\033\[[0-9;]*m", "", s)


def _emoji_extra_width(s: str) -> int:
    """Count extra width consumed by double-wide emoji characters."""
    # Basic heuristic: code points in common emoji ranges add 1 extra cell
    count = 0
    for ch in s:
        cp = ord(ch)
        if (
            0x1F000 <= cp <= 0x1FFFF  # misc symbols / emoji
            or 0x2600 <= cp <= 0x26FF  # misc symbols
            or 0x2700 <= cp <= 0x27BF  # dingbats
            or 0x231A <= cp <= 0x231B  # watch / hourglass
            or 0x23E9 <= cp <= 0x23F3
            or 0x25AA <= cp <= 0x25FE
            or 0x2614 <= cp <= 0x2615
            or 0x2648 <= cp <= 0x2653
            or 0x267F == cp
            or 0x2693 == cp
            or 0x26A1 == cp
            or 0x26CE == cp
            or 0x26D4 == cp
            or 0x26EA == cp
            or 0x26F2 <= cp <= 0x26F3
            or 0x26F5 == cp
            or 0x26FA == cp
            or 0x26FD == cp
        ):
            count += 1
    return count


def display_width(s: str) -> int:
    """Return the display width of s accounting for ANSI codes and emoji."""
    clean = strip_ansi(s)
    return len(clean) + _emoji_extra_width(clean)


def truncate(s: str, max_w: int) -> str:
    """Truncate s to at most max_w display cells, appending … if needed."""
    if display_width(s) <= max_w:
        return s
    # Walk the string, skipping ANSI escape sequences for width counting
    result = ""
    width = 0
    i = 0
    while i < len(s):
        # Detect ANSI escape sequence
        if s[i] == "\033" and i + 1 < len(s) and s[i + 1] == "[":
            j = i + 2
            while j < len(s) and (s[j].isdigit() or s[j] == ";"):
                j += 1
            if j < len(s) and s[j] == "m":
                # Copy the entire ANSI sequence without counting width
                result += s[i : j + 1]
                i = j + 1
                continue
        ch = s[i]
        ch_w = 1 + (1 if _emoji_extra_width(ch) > 0 else 0)
        if width + ch_w > max_w - 1:
            return result + "…"
        result += ch
        width += ch_w
        i += 1
    return result


def pad_cell(s: str, width: int) -> str:
    """Pad s with trailing spaces so display width == width."""
    dw = display_width(s)
    if dw < width:
        return s + " " * (width - dw)
    return s


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------

def column_widths(total_cols: int) -> tuple[int, int, int]:
    """Return (w_topics, w_actions, w_agents) for total_cols terminal width.

    Content rows use shared borders: │ col1 │ col2 │ col3 │ = 4 border chars.
    """
    ratios = _get_settings()["cockpit"]["column_ratios"]  # [topics, actions, agents]
    usable = total_cols - 4  # 4 │ borders (shared)
    w_topics = int(usable * ratios[0])
    w_agents = int(usable * ratios[2])
    w_actions = usable - w_topics - w_agents  # absorbs rounding
    return w_topics, w_actions, w_agents


# ---------------------------------------------------------------------------
# Header / footer builders
# ---------------------------------------------------------------------------

def make_header_row(titles: list[str], widths: list[int]) -> str:
    """Build ╭─ T1 ──┬─ T2 ──┬─ T3 ──╮  totaling sum(widths)+4 = terminal width."""
    parts = []
    for title, w in zip(titles, widths):
        # Each part is exactly w chars (matches the cell width between │ borders)
        inner = f"─ {title} "
        fill = w - len(inner)
        parts.append(inner + "─" * max(0, fill))
    return "╭" + "┬".join(parts) + "╮"


def make_footer_row(widths: list[int]) -> str:
    """Build ╰──────┴────────┴──────╯  totaling sum(widths)+4 = terminal width."""
    parts = ["─" * w for w in widths]
    return "╰" + "┴".join(parts) + "╯"


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds // 3600)}h"


# ---------------------------------------------------------------------------
# Priority tier calculation (Feature 2)
# ---------------------------------------------------------------------------

def _get_priority_tier(thread: dict, current_id: str | None, state_emoji: str) -> int:
    """Return numeric priority tier for a thread. Lower = higher priority."""
    tid = thread["id"]
    status = thread.get("status") or "active"
    agent_result = thread.get("agent_result") or ""

    # BLOCKER: agent_result starts with ⚠️ BLOCKER:
    if agent_result.startswith("⚠️ BLOCKER:"):
        return TIER_BLOCKER

    # REVIEW: done + has result + not current + not yet reviewed
    if status == "done" and agent_result and tid != current_id and not thread.get("reviewed"):
        return TIER_REVIEW

    # ACTIVE: background agent running
    if status == "background":
        return TIER_ACTIVE

    # CURRENT
    if tid == current_id:
        return TIER_CURRENT

    # WAITING: emoji returned is ⏸️
    if "⏸️" in state_emoji:
        return TIER_WAITING

    # IDLE: last_active > 2h
    age = _thread_age_seconds(thread.get("last_active"))
    if age is not None and age > 2 * 3600:
        return TIER_IDLE

    # DONE
    if status == "done":
        return TIER_DONE

    return TIER_IDLE


# ---------------------------------------------------------------------------
# Topics column renderer
# ---------------------------------------------------------------------------

def render_topics_column(
    threads: list[dict],
    current_id: str | None,
    db: "JuggleDB",
    content_w: int,
) -> list[str]:
    """Return list of content strings (no padding) for topics column."""
    rows: list[str] = []

    visible = [
        t for t in threads
        if t.get("show_in_list", 1) != 0 and t.get("status") != "archived"
    ]

    # Compute state emoji and tier per thread
    enriched = []
    for t in visible:
        emoji = get_thread_state(db, t, current_id or "")
        tier = _get_priority_tier(t, current_id, emoji)
        enriched.append((tier, t, emoji))

    enriched.sort(key=lambda x: x[0])

    for tier, thread, emoji in enriched:
        label = thread.get("label") or "?"
        title = thread.get("title") or thread.get("topic") or "?"
        status = thread.get("status") or "active"

        # Duration for background threads
        duration_str = ""
        if status == "background":
            age = _thread_age_seconds(thread.get("last_active"))
            if age is not None:
                duration_str = f" {YELLOW}{_fmt_duration(age)}{RESET}"

        prefix = f"{emoji} [{label}] "
        prefix_w = display_width(prefix)
        dur_w = display_width(strip_ansi(duration_str)) + (1 if duration_str else 0)
        title_max = content_w - prefix_w - dur_w
        title_trunc = truncate(title, max(1, title_max))

        line = prefix + title_trunc + duration_str

        # Apply tier colors
        if tier == TIER_BLOCKER:
            line = f"{RED}{line}{RESET}"
        elif tier == TIER_REVIEW:
            line = f"{YELLOW}{line}{RESET}"
        elif tier == TIER_ACTIVE:
            line = f"{GREEN}{line}{RESET}"
        elif tier == TIER_CURRENT:
            line = f"{BOLD}{WHITE}{line}{RESET}"
        elif tier in (TIER_IDLE, TIER_DONE):
            line = f"{DIM}{line}{RESET}"

        rows.append(line)

    return rows


# ---------------------------------------------------------------------------
# Agents column renderer
# ---------------------------------------------------------------------------

def render_agents_column(
    agents: list[dict],
    content_w: int,
) -> list[str]:
    """Return list of content strings for agents column."""
    if not agents:
        return [f"{DIM}no agents{RESET}"]

    def _agent_sort_key(a: dict) -> int:
        s = a.get("status") or ""
        if s == "busy":
            return 0
        if s == "decommission_pending":
            return 1
        return 2

    sorted_agents = sorted(agents, key=_agent_sort_key)

    rows: list[str] = []
    for agent in sorted_agents:
        a_status = agent.get("status") or "idle"
        short_id = (agent.get("id") or "????")[:4]
        role = (agent.get("role") or "")[:10]
        thread_label = agent.get("assigned_thread_label") or "—"
        age = _thread_age_seconds(agent.get("last_active"))

        if a_status == "busy":
            dot = "🟢"
            duration = _fmt_duration(age)
        elif a_status == "decommission_pending":
            dot = "⚠️"
            duration = _fmt_duration(age)
        else:
            dot = "💤"
            # Show — for idle >5m
            duration = "—" if (age is None or age > 300) else _fmt_duration(age)

        label_display = f"[{thread_label}]" if thread_label != "—" else " — "
        line = f"{dot} {label_display} {short_id}  {role:<10}  {duration}"
        line = truncate(line, content_w)

        if a_status == "busy":
            line = f"{GREEN}{line}{RESET}"
        elif a_status == "idle":
            line = f"{DIM}{line}{RESET}"
        elif a_status == "decommission_pending":
            line = f"{YELLOW}{line}{RESET}"

        rows.append(line)

    return rows


# ---------------------------------------------------------------------------
# Action Items column renderer (Feature 3 nudges)
# ---------------------------------------------------------------------------

def _extract_blocker_text(agent_result: str | None) -> str | None:
    if agent_result and agent_result.startswith("⚠️ BLOCKER:"):
        return agent_result[len("⚠️ BLOCKER:"):].strip()
    return None


def render_actions_column(
    threads: list[dict],
    current_id: str | None,
    content_w: int,
) -> list[str]:
    """Return list of content strings for action items column."""
    rows: list[str] = []

    visible = [
        t for t in threads
        if t.get("show_in_list", 1) != 0 and t.get("status") != "archived"
    ]

    # 1. Blockers
    for thread in visible:
        blocker_text = _extract_blocker_text(thread.get("agent_result"))
        if blocker_text:
            label = thread.get("label") or "?"
            prefix = f"⚠️ [{label}] "
            text = truncate(prefix + blocker_text, content_w)
            rows.append(f"{RED}{text}{RESET}")

    # 2. Nudges — capped at 3 total lines
    nudges: list[str] = []

    # Review nudge: done + result + not current → group by label
    review_labels = []
    for thread in visible:
        status = thread.get("status") or "active"
        agent_result = thread.get("agent_result") or ""
        if (status == "done" and agent_result
                and not agent_result.startswith("⚠️ BLOCKER:") and not thread.get("reviewed")):
            review_labels.append(thread.get("label") or "?")

    if review_labels:
        labels_str = " ".join(f"[{lb}]" for lb in review_labels)
        nudge = f"📬 {labels_str} agent finished — results ready"
        nudges.append(f"{YELLOW}{truncate(nudge, content_w)}{RESET}")

    # Idle-with-open-question nudge
    idle_oq_labels = []
    for thread in visible:
        oq = json.loads(thread.get("open_questions") or "[]")
        if not oq:
            continue
        age = _thread_age_seconds(thread.get("last_active"))
        if age is not None and age > _get_settings()["cockpit"]["idle_open_question_threshold_secs"]:
            idle_oq_labels.append((thread.get("label") or "?", age))

    if idle_oq_labels:
        labels_str = " ".join(f"[{lb}]" for lb, _ in idle_oq_labels)
        nudge = f"💬 {labels_str} idle with open questions"
        nudges.append(f"{YELLOW}{truncate(nudge, content_w)}{RESET}")

    # Stale blocker nudge (>4h)
    stale_labels = []
    for thread in visible:
        blocker_text = _extract_blocker_text(thread.get("agent_result"))
        if not blocker_text:
            continue
        age = _thread_age_seconds(thread.get("last_active"))
        if age is not None and age > _get_settings()["cockpit"]["stale_blocker_threshold_secs"]:
            stale_labels.append((thread.get("label") or "?", age))

    if stale_labels:
        for lb, age in stale_labels:
            nudge = f"🔴 [{lb}] blocker unaddressed {_fmt_duration(age)}"
            nudges.append(f"{RED}{truncate(nudge, content_w)}{RESET}")

    rows.extend(nudges[:_get_settings()["cockpit"]["max_nudge_lines"]])

    # 3. Open questions
    for thread in visible:
        oq_list = json.loads(thread.get("open_questions") or "[]")
        label = thread.get("label") or "?"
        for oq in oq_list:
            prefix = f"❓ [{label}] "
            text = truncate(prefix + str(oq), content_w)
            rows.append(f"{YELLOW}{text}{RESET}")

    if not rows:
        rows.append(f"{DIM}{GREEN}✓ no blockers or open questions{RESET}")

    return rows


# ---------------------------------------------------------------------------
# Notifications column renderer
# ---------------------------------------------------------------------------

_NOTIF_STYLE = {
    "info":    (WHITE, "✓"),
    "warning": (YELLOW, "⚠"),
    "error":   (RED, "✗"),
}


def render_notifications_column(
    db: "JuggleDB",
    content_w: int,
    max_rows: int = 4,
) -> list[str]:
    """Return content strings for notification pane (non-action severity only)."""
    conn = db._connect()
    rows = conn.execute(
        """
        SELECT n.*, t.label as thread_label
        FROM notifications n
        LEFT JOIN threads t ON n.thread_id = t.id
        WHERE n.severity != 'action'
        ORDER BY n.id DESC LIMIT ?
        """,
        (max_rows,),
    ).fetchall()
    notifs = [dict(row) for row in rows]

    if not notifs:
        return [f"{DIM}no notifications{RESET}"]

    result = []
    for n in notifs:
        sev = n.get("severity") or "info"
        color, icon = _NOTIF_STYLE.get(sev, (WHITE, "✓"))
        label = n.get("thread_label") or "?"
        msg = n.get("message") or ""
        line = f"{icon} [{label}] {msg}"
        result.append(f"{color}{truncate(line, content_w)}{RESET}")

    return result


# ---------------------------------------------------------------------------
# Frame assembly
# ---------------------------------------------------------------------------

def render_frame(cols: int, rows: int, db: "JuggleDB | None" = None, db_path: str | None = None) -> str:
    """Render a complete cockpit frame as a string."""
    if db is None:
        db = JuggleDB(db_path=db_path)
    all_threads = db.get_all_threads()
    all_agents = db.get_all_agents()
    current_id = db.get_current_thread()

    w_topics, w_actions, w_agents = column_widths(cols)
    content_topics = w_topics - 2   # 1 space padding each side
    content_actions = w_actions - 2
    content_agents = w_agents - 2
    display_rows = max(1, rows - 3)  # header + footer + 1 margin

    notif_max_rows = _get_settings()["cockpit"]["max_notification_rows"]
    actions_display_rows = max(0, display_rows - notif_max_rows - 1)  # -1 for divider row

    # Render columns
    topics_lines = render_topics_column(all_threads, current_id, db, content_topics)
    agents_lines = render_agents_column(all_agents, content_agents)
    actions_lines = render_actions_column(all_threads, current_id, content_actions)
    notif_lines = render_notifications_column(db, content_actions, max_rows=notif_max_rows)

    # Pad each column
    def _pad_col(lines: list[str], count: int) -> list[str]:
        padded = list(lines[:count])
        while len(padded) < count:
            padded.append("")
        return padded

    topics_lines = _pad_col(topics_lines, display_rows)
    actions_lines = _pad_col(actions_lines, actions_display_rows)
    agents_lines = _pad_col(agents_lines, display_rows)
    notif_lines = _pad_col(notif_lines, notif_max_rows)

    # Divider content for middle column (fills w_actions chars)
    divider_title = "─ NOTIFICATIONS "
    divider_inner = divider_title + "─" * max(0, w_actions - len(divider_title))

    # Assemble combined middle column: actions + divider + notifications
    middle_lines = actions_lines + [divider_inner] + notif_lines
    divider_row_idx = actions_display_rows  # index where divider sits

    # Build output
    out_lines: list[str] = []

    # Headers
    header = make_header_row(
        ["TOPICS", "ACTION ITEMS", "AGENTS"],
        [w_topics, w_actions, w_agents],
    )
    out_lines.append(header)

    # Content rows
    for i in range(display_rows):
        tc = " " + pad_cell(truncate(topics_lines[i], content_topics), content_topics) + " "
        agc = " " + pad_cell(truncate(agents_lines[i], content_agents), content_agents) + " "
        if i == divider_row_idx:
            # Divider row spans middle column with ├ and ┤ borders
            out_lines.append("│" + tc + "├" + divider_inner + "┤" + agc + "│")
        else:
            mid_content = middle_lines[i] if i < len(middle_lines) else ""
            ac = " " + pad_cell(truncate(mid_content, content_actions), content_actions) + " "
            out_lines.append("│" + tc + "│" + ac + "│" + agc + "│")

    # Footers
    footer = make_footer_row([w_topics, w_actions, w_agents])
    out_lines.append(footer)

    return "\n".join(out_lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _make_cockpit_db(db_path: str | None = None) -> JuggleDB:
    """Create a JuggleDB with a persistent connection for cockpit use.

    Normal JuggleDB._connect() creates a new connection each call. In a 1s
    refresh loop that leaks file descriptors. We cache one connection and
    return it on every _connect() call.
    """
    import sqlite3 as _sqlite3

    db = JuggleDB(db_path=db_path)
    db.init_db()  # run migrations before monkey-patching the connection
    conn = _sqlite3.connect(str(db.db_path))
    conn.row_factory = _sqlite3.Row
    db._connect = lambda: conn  # noqa: E731 — intentional monkey-patch
    return db


def run(db_path: str | None = None) -> None:
    """Start the cockpit refresh loop."""
    db = _make_cockpit_db(db_path)
    if not db.is_active():
        print("Juggle inactive. Run /juggle:start first.")
        sys.exit(1)

    cols, rows = shutil.get_terminal_size()
    if cols < 80:
        print("Terminal too narrow (need 80+ cols).")
        sys.exit(1)

    try:
        from juggle_tmux import JuggleTmuxManager
        _cockpit_mgr = JuggleTmuxManager()
    except Exception:
        _cockpit_mgr = None

    while True:
        try:
            cols, rows = shutil.get_terminal_size()
            if _cockpit_mgr is not None:
                _throttled_reaper(db, _cockpit_mgr)
            frame = render_frame(cols, rows, db=db)
            sys.stdout.write("\033[H\033[J" + frame)
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(f"\033[H\033[J{RED}[error] {e}{RESET}\n")
            sys.stdout.flush()
        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    import argparse

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    parser = argparse.ArgumentParser(description="Juggle Cockpit dashboard")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="Path to juggle.db file")
    args = parser.parse_args()
    run(db_path=args.db_path)
