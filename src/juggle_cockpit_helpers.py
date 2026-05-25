"""Juggle Cockpit — pure module-level helpers and constants.

Extracted from juggle_cockpit.py for unit-testability without Textual.
All symbols are re-exported from juggle_cockpit for backward compatibility.
"""

from __future__ import annotations

import subprocess

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCROLL_PANES = ("actions", "agents", "notifications")

_PRIORITY_TIER_MAP: dict[str, int] = {
    "high": 0, "blocker": 0,
    "normal": 1, "review": 1,
    "low": 2, "note": 2,
}


# ---------------------------------------------------------------------------
# Thread / action / agent resolution
# ---------------------------------------------------------------------------


def _resolve_thread_by_label(threads: list[dict], label: str) -> dict | None:
    """Return the first thread dict whose user_label matches label (case-insensitive)."""
    label_up = label.upper()
    return next(
        (t for t in threads if (t.get("user_label") or "").upper() == label_up),
        None,
    )


def _resolve_actions_by_thread_label(
    threads: list[dict], open_actions: list[dict], label: str
) -> list[dict]:
    """Return all open action dicts whose thread_id belongs to the named thread."""
    thread = _resolve_thread_by_label(threads, label)
    if thread is None:
        return []
    thread_id = thread.get("id")
    return [a for a in open_actions if a.get("thread_id") == thread_id]


def _resolve_agent_by_index(agents: list, index_1based: int):
    """Return the Agent at 1-based position, or None if out of range."""
    idx = index_1based - 1
    if idx < 0 or idx >= len(agents):
        return None
    return agents[idx]


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def _parse_filter(text: str) -> tuple[str | None, str]:
    """Parse filter text into (priority_key_or_None, substring).

    'priority:high foo' → ('high', 'foo')
    'foo bar'           → (None, 'foo bar')
    'priority:blocker'  → ('blocker', '')
    ''                  → (None, '')
    """
    if text.startswith("priority:"):
        rest = text[len("priority:"):].strip()
        parts = rest.split(None, 1)
        key = parts[0] if parts else ""
        sub = parts[1] if len(parts) > 1 else ""
        return key, sub
    return None, text


def _apply_filter_actions(actions: list, text: str) -> list:
    """Filter Action list. Empty text returns the same list object (fast path).

    Supports 'priority:<key> [substring]' prefix.
    Keys: high/blocker → tier 0; normal/review → tier 1; low/note → tier 2.
    Substring matches action.text and action.topic_id (case-insensitive).
    """
    if not text:
        return actions
    priority_key, substring = _parse_filter(text)
    result = actions
    if priority_key is not None:
        tier = _PRIORITY_TIER_MAP.get(priority_key.lower())
        if tier is not None:
            result = [a for a in result if a.tier == tier]
    if substring:
        low = substring.lower()
        result = [
            a for a in result
            if low in a.text.lower() or low in (a.topic_id or "").lower()
        ]
    return result


def _apply_filter_text(items: list, text: str) -> list:
    """Generic text-substring filter for Agent and Notification lists.

    Matches against .text, .role, and .topic_id attributes (whichever exist).
    Empty text returns the same list object (fast path).
    """
    if not text:
        return items
    low = text.lower()
    return [
        item for item in items
        if low in getattr(item, "text", "").lower()
        or low in getattr(item, "role", "").lower()
        or low in (getattr(item, "topic_id", None) or "").lower()
    ]


# ---------------------------------------------------------------------------
# Bell / desktop notification diff helpers
# ---------------------------------------------------------------------------


def _new_blocker_actions(prev_ids: set[str], current_actions: list) -> list:
    """Return tier-0 (blocker) actions whose id was not in prev_ids."""
    return [a for a in current_actions if a.tier == 0 and a.id not in prev_ids]


def _newly_failed_agents(prev_statuses: dict[str, str], current_agents: list) -> list:
    """Return agents that transitioned *to* 'stale' from a known non-stale status.

    Agents with no prior entry (new agents) are skipped to avoid false alerts
    on cockpit startup when existing stale agents are first seen.
    """
    return [
        a for a in current_agents
        if a.status == "stale"
        and a.id_short in prev_statuses           # must have been seen before
        and prev_statuses[a.id_short] != "stale"  # and was not already stale
    ]


def _send_desktop_notification(title: str, body: str) -> None:
    """Fire-and-forget macOS desktop notification via osascript.

    Non-blocking: uses Popen. Silently no-ops on non-macOS or missing osascript.
    Caller must sanitize title/body to avoid shell injection (no user input allowed).
    """
    body_safe = body[:120].replace('"', "'")
    title_safe = title[:60].replace('"', "'")
    script = f'display notification "{body_safe}" with title "{title_safe}"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # osascript unavailable


# ---------------------------------------------------------------------------
# tmux pane helpers
# ---------------------------------------------------------------------------


def _tmux_focus_pane(pane_id: str) -> bool:
    """Run `tmux select-pane -t <pane_id>`. Returns True on success."""
    try:
        result = subprocess.run(
            ["tmux", "select-pane", "-t", pane_id],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _tmux_capture_pane(pane_id: str, lines: int = 20) -> str:
    """Capture last N lines of tmux pane output. Returns '' on failure."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", pane_id],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return ""
        captured = result.stdout.splitlines()
        return "\n".join(captured[-lines:]) if captured else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
