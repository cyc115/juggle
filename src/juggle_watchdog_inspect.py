"""juggle_watchdog_inspect — inspect_agent entry point and crash handler.

Owns: inspect_agent (single-agent pane inspection + action dispatch),
_handle_crashed, _config_dir helper.
Must not own: batch recovery (execute_recovery), orphan scanning
(check_orphaned_threads), classifier constants (see juggle_watchdog.py).

All names are re-exported by juggle_watchdog.py so existing imports
``from juggle_watchdog import inspect_agent`` continue to work.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time as _time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)


def _config_dir() -> Path:
    from juggle_settings import get_settings

    return Path(get_settings()["paths"]["config_dir"])


def inspect_agent(agent_id: str, db: Any, _tmux_session: str) -> dict:
    """Inspect a single agent's tmux pane and take action based on state.

    Args:
        agent_id: UUID of the agent to inspect.
        db: JuggleDB instance (never read CLAUDE_PLUGIN_DATA internally).
        tmux_session: tmux session name (never hardcoded).

    Returns:
        {
            'state': 'working' | 'recoverable_prompt' | 'stalled_silent' | 'crashed' | 'stuck_at_prompt',
            'actions': list[str],
            'action_item_id': int | None,
            'notification_id': int | None,
        }
    """
    # Import classifier constants from juggle_watchdog to avoid duplication.
    # juggle_watchdog imports this module last so the circular reference resolves.
    import juggle_watchdog as _wdog

    from datetime import datetime, timezone

    agent = db.get_agent(agent_id)
    if agent is None:
        return {
            "state": "crashed",
            "actions": ["agent_missing"],
            "action_item_id": None,
            "notification_id": None,
        }

    pane_id = agent["pane_id"]
    thread_id = agent.get("assigned_thread")
    label = _wdog._get_thread_label(db, thread_id) if thread_id else agent_id[:8]
    session_id = _wdog.get_session_id(db)

    stall_threshold = float(os.environ.get("JUGGLE_WATCHDOG_STALL_SECS", "60"))

    config_dir = _config_dir()
    snapshot_dir = config_dir / "watchdog" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Capture pane content
    cap = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id],
        capture_output=True,
        text=True,
    )
    if cap.returncode != 0:
        raw_content = None
    else:
        raw_content = cap.stdout

    result: dict = {
        "state": "working",
        "actions": [],
        "action_item_id": None,
        "notification_id": None,
    }

    # Non-interactive (one-shot) agent: skip pane-marker classification entirely.
    # Use PID-based liveness instead — pane markers (Claude UI) are meaningless.
    if _wdog._agent_is_non_interactive(agent):
        from juggle_tmux import oneshot_agent_alive as _oneshot_alive
        if _oneshot_alive(agent):
            result["state"] = "working"
            return result
        # Dead one-shot: reconcile via the same path as crashed.
        if raw_content is None:
            return _handle_crashed(
                db, agent, thread_id, label, session_id, result,
                pane_content="", snapshot_dir=snapshot_dir,
            )
        # Process died but pane still exists — treat as crashed (shell prompt).
        return _handle_crashed(
            db, agent, thread_id, label, session_id, result,
            pane_content=_wdog._strip_ansi(raw_content), snapshot_dir=snapshot_dir,
        )

    if raw_content is None:
        # Pane gone entirely
        return _handle_crashed(
            db,
            agent,
            thread_id,
            label,
            session_id,
            result,
            pane_content="",
            snapshot_dir=snapshot_dir,
        )

    content = _wdog._strip_ansi(raw_content)
    _lines = content.splitlines()
    while _lines and not _lines[-1].strip():
        _lines.pop()
    tail = "\n".join(_lines[-15:])

    # 1. Allowlist prompts — check both single-line format and multiline fixture format
    matched_key: str | None = None
    for pattern, key in _wdog._ALLOWLIST:
        if pattern in tail:
            matched_key = key
            break
    # Flexible: detect multiline "1. Yes ... 2. Yes ... 3. No" permission dialog
    if (
        matched_key is None
        and re.search(r"1\.\s+Yes", tail)
        and re.search(r"2\.\s+Yes", tail)
    ):
        matched_key = "2"

    if matched_key is not None:
        result["state"] = "recoverable_prompt"
        result["actions"].append("sent_key")
        if matched_key:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, matched_key, "Enter"],
                capture_output=True,
            )
        else:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True
            )
        notif_id = db.add_notification_v2(
            thread_id=thread_id,
            message=f"[Watchdog] [{label}] auto-resolved permission prompt (key={matched_key!r})",
            session_id=session_id,
        )
        result["notification_id"] = notif_id
        return result

    # 2. Shell prompt (crash) — check suffix AND shell-specific indicators
    last_nonempty = next(
        (line for line in reversed(content.splitlines()) if line.strip()), ""
    )
    is_shell_prompt = any(
        last_nonempty.endswith(suffix) for suffix in _wdog._SHELL_SUFFIXES
    ) or any(indicator in last_nonempty for indicator in _wdog._SHELL_INDICATORS)
    if is_shell_prompt:
        return _handle_crashed(
            db,
            agent,
            thread_id,
            label,
            session_id,
            result,
            pane_content=content,
            snapshot_dir=snapshot_dir,
        )

    # 3. ╭─╮ box → stuck_at_prompt (send Enter to unblock)
    if _wdog._has_box_top(content):
        result["state"] = "stuck_at_prompt"
        result["actions"].append("sent_enter")
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True
        )
        notif_id = db.add_notification_v2(
            thread_id=thread_id,
            message=f"[Watchdog] [{label}] stuck-at-prompt — sent Enter to unblock",
            session_id=session_id,
        )
        result["notification_id"] = notif_id
        return result

    # 4. Stall detection — compare stall_for against threshold
    # Guard: orchestrator owns undispatched agents; watchdog must not recover them.
    if agent.get("last_send_task_at") is None:
        result["state"] = "awaiting_dispatch"
        return result

    last_active_str = agent.get("last_active") or agent.get("last_active_at")
    stall_for = 0.0
    if last_active_str:
        try:
            last_dt = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            stall_for = (datetime.now(timezone.utc) - last_dt).total_seconds()
        except (ValueError, TypeError):
            stall_for = 0.0

    if stall_for >= stall_threshold and "Thinking" not in tail:
        result["state"] = "stalled_silent"
        result["actions"].append("filed_action_item")
        ts = int(_time.time())
        snap_path = snapshot_dir / f"{agent_id}-{ts}.txt"
        snap_path.write_text(content)
        item_id = db.add_action_item(
            thread_id=thread_id,
            message=(
                f"🚨 [{label}] agent stalled (silent for {int(stall_for)}s) — "
                f"snapshot at {snap_path}"
            ),
            type_="failure",
            priority="high",
        )
        result["action_item_id"] = item_id
        return result

    # 5. Working
    result["state"] = "working"
    return result


def _handle_crashed(
    db: Any,
    agent: dict,
    thread_id: str | None,
    label: str,
    session_id: str,
    result: dict,
    pane_content: str,
    snapshot_dir: Path,
) -> dict:
    result["state"] = "crashed"
    result["actions"].append("filed_action_item")

    if thread_id:
        db.update_thread(thread_id, status="failed")

    db.update_agent(agent["id"], status="idle", assigned_thread=None)

    item_id = db.add_action_item(
        thread_id=thread_id,
        message=f"🚨 [{label}] agent crashed — pane exited or shell prompt detected",
        type_="failure",
        priority="high",
    )
    result["action_item_id"] = item_id
    return result
