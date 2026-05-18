"""Juggle agent watchdog — pure functions and inspect_agent for the watchdog daemon."""
from __future__ import annotations

import hashlib as _hashlib
import logging
import os
import re
import subprocess
import time as _time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_ALLOWLIST: list[tuple[str, str]] = [
    ("1. Yes / 2. Yes, allow always / 3. No", "2"),
    ("1. Yes, auto-accept / 2. Yes, manually approve / 3. No", "2"),
    ("Press Enter to continue", ""),
]

_SHELL_SUFFIXES = ("$ ", "% ", "> ", "❯ ")
_SHELL_INDICATORS = ("in zsh", "in bash", "in fish")
_COLD_START_DEFAULTS: dict[str, float] = {
    "coder": 300.0,
    "planner": 180.0,
    "researcher": 120.0,
}
_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_TOP_RE = re.compile(r"^╭─+╮\s*$")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _hash_tail(content: str, lines: int = 10) -> str:
    tail = "\n".join(content.splitlines()[-lines:])
    return _hashlib.sha256(tail.encode()).hexdigest()[:16]


def _has_execution_markers(tail: str) -> bool:
    return any(m in tail for m in _EXECUTION_MARKERS)


def _has_box_top(content: str) -> bool:
    return any(_BOX_TOP_RE.match(line) for line in content.splitlines())


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def read_snapshot(agent_id: str, snapshot_dir: Path) -> str | None:
    path = snapshot_dir / f"{agent_id}.txt"
    return path.read_text() if path.exists() else None


def write_snapshot(agent_id: str, content: str, snapshot_dir: Path) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{agent_id}.txt").write_text(content)


def write_recovery_snapshot(agent_id: str, content: str, recovery_dir: Path) -> Path:
    """Write a recovery snapshot; prune to last 100 per agent (DA-4 fix)."""
    recovery_dir.mkdir(parents=True, exist_ok=True)
    ts = _time.time_ns()  # nanosecond precision avoids collisions in rapid succession
    path = recovery_dir / f"{agent_id}-{ts}.txt"
    path.write_text(content)
    agent_snaps = sorted(recovery_dir.glob(f"{agent_id}-*.txt"),
                         key=lambda p: p.stat().st_mtime)
    for old in agent_snaps[:-100]:
        try:
            old.unlink()
        except FileNotFoundError:
            pass
    return path


# ---------------------------------------------------------------------------
# State classifier (pure, used by daemon poll loop)
# ---------------------------------------------------------------------------

def classify_pane_state(
    content: str | None,
    prev_content: str | None,
    stalled_for: float,
    threshold: float,
    *,
    last_send_task_pane_hash: str | None = None,
) -> tuple[str, str | None]:
    """Classify agent pane state. Returns (state, key_to_send).

    States: working | crashed | prompt | stuck | quiet | stalled
    Classification order (most specific first).
    """
    if content is None:
        return "crashed", None

    _cls_lines = content.splitlines()
    while _cls_lines and not _cls_lines[-1].strip():
        _cls_lines.pop()
    tail = "\n".join(_cls_lines[-15:])

    for pattern, key in _ALLOWLIST:
        if pattern in tail:
            return "prompt", key

    last_nonempty = next(
        (line for line in reversed(content.splitlines()) if line.strip()), ""
    )
    if any(last_nonempty.endswith(suffix) for suffix in _SHELL_SUFFIXES):
        return "crashed", None

    if content != prev_content:
        return "working", None

    if (
        last_send_task_pane_hash is not None
        and stalled_for >= 60
        and not _has_execution_markers(tail)
        and _hash_tail(content) == last_send_task_pane_hash
    ):
        return "stuck", None

    if "Thinking" in tail or stalled_for < 60:
        return "quiet", None

    if stalled_for >= threshold:
        return "stalled", None

    return "quiet", None


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

def get_threshold_seconds(db: Any, agent: dict) -> float:
    override = agent.get("watchdog_threshold_minutes")
    if override is not None:
        if override == -1:
            return float("inf")
        if override > 0:
            return float(override) * 60.0

    role = agent.get("role", "researcher")
    median = db.get_median_duration_secs(role)
    if median is not None:
        return 2.0 * median

    return _COLD_START_DEFAULTS.get(role, 180.0)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def get_session_id(db: Any) -> str:
    with db._connect() as conn:
        row = conn.execute("SELECT value FROM session WHERE key='session_id'").fetchone()
    return row["value"] if row else ""


def _get_thread_label(db: Any, thread_id: str) -> str:
    if not thread_id:
        return "unknown"
    thread = db.get_thread(thread_id)
    if not thread:
        return thread_id[:8]
    return thread.get("user_label") or thread.get("label") or thread_id[:8]


# ---------------------------------------------------------------------------
# Prompt auto-resolution
# ---------------------------------------------------------------------------

def handle_prompt(db: Any, mgr: Any, agent: dict, pane_id: str, key: str) -> None:
    if key:
        mgr._run_tmux("send-keys", "-t", pane_id, key, "Enter")
    else:
        mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
    thread_id = agent.get("assigned_thread")
    label = _get_thread_label(db, thread_id) if thread_id else agent["id"][:8]
    session_id = get_session_id(db)
    db.add_notification_v2(
        thread_id=thread_id,
        message=f"[Watchdog] [{label}] auto-resolved permission prompt (key={key!r})",
        session_id=session_id,
    )
    _log.info("Watchdog: prompt resolved for agent %s key=%r", agent["id"][:8], key)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def execute_recovery(
    db: Any,
    mgr: Any,
    agent: dict,
    pane_content: str,
    *,
    recovery_dir: Path,
    session_id: str,
) -> None:
    """Decommission a stalled/crashed agent and (if eligible) re-dispatch it."""
    agent_id = agent["id"]

    # DA-6: Recheck agent status from DB to guard against TOCTOU race; use live
    # record for all subsequent reads so a concurrent release can't mislead us.
    live = db.get_agent(agent_id)
    if live is None or live.get("status") != "busy":
        _log.info("Watchdog: recovery aborted for %s — agent no longer busy", agent_id[:8])
        return

    thread_id = live.get("assigned_thread")
    role = live.get("role", "researcher")
    model = live.get("model")
    last_task = live.get("last_task")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    snap_path = write_recovery_snapshot(agent_id, pane_content, recovery_dir)
    _log.info("Watchdog: recovery snapshot saved to %s", snap_path)

    if thread_id:
        with db._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_dispatched_task=?, last_dispatched_role=?, "
                "last_dispatched_model=? WHERE id=?",
                (last_task, role, model, thread_id),
            )
            conn.commit()

    # Kill pane (best-effort) then delete agent from DB directly
    try:
        mgr.kill_pane(live["pane_id"])
    except Exception:
        pass
    db.delete_agent(agent_id)

    if thread_id:
        db.update_thread(thread_id, status="failed")

    if live.get("watchdog_retried", 0) >= 1:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=(f"🛑 [{label}] agent stalled AGAIN after watchdog retry — "
                         f"manual intervention required. Snapshot: {snap_path}"),
                type_="failure", priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="retry_blocked", snapshot_path=str(snap_path))
        return

    if not last_task:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=(f"🚨 [{label}] agent stalled — no task content to replay; "
                         f"re-dispatch manually. Snapshot: {snap_path}"),
                type_="failure", priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="stalled", snapshot_path=str(snap_path))
        return

    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=(f"🚨 [{label}] agent stalled/crashed — snapshot at {snap_path}, "
                     f"auto-retrying"),
            type_="failure", priority="high",
        )

    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(new_agent_id, status="busy", assigned_thread=thread_id,
                    last_active=now, busy_since=now, watchdog_retried=1, last_task=last_task)
    if thread_id:
        db.update_thread(thread_id, status="background")

    mgr.send_task(new_pane_id, last_task)

    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=(f"⚠️ [{label}] agent auto-re-dispatched after stall — "
                     f"verify result when complete"),
            type_="manual_step", priority="normal",
        )

    db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                          event_type="recovered", snapshot_path=str(snap_path))
    _log.info("Watchdog: re-dispatched %s → %s for thread %s",
              agent_id[:8], new_agent_id[:8], (thread_id or "")[:8])


# ---------------------------------------------------------------------------
# Orphaned thread detection — Loop 2
# ---------------------------------------------------------------------------

def check_orphaned_threads(
    db: Any,
    *,
    orphan_threshold: float = 300.0,
    dedup_window_hours: float = 24.0,
) -> list[str]:
    """Scan background threads with no active agent; file action items for orphans.

    Returns list of orphaned thread_ids detected this cycle. Uses 24h dedup guard.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    dedup_cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    with db._connect() as conn:
        thread_rows = conn.execute(
            "SELECT * FROM threads WHERE status='background'"
        ).fetchall()
        threads = [dict(r) for r in thread_rows]
        busy_rows = conn.execute(
            "SELECT assigned_thread FROM agents WHERE status='busy' AND assigned_thread IS NOT NULL"
        ).fetchall()
        busy_thread_ids = {r["assigned_thread"] for r in busy_rows}

    orphaned: list[str] = []

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in busy_thread_ids:
            continue

        last_active_at = thread.get("last_active_at")
        if not last_active_at:
            continue

        try:
            last_dt = datetime.fromisoformat(last_active_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            orphaned_for = (now - last_dt).total_seconds()
        except (ValueError, TypeError):
            continue

        if orphaned_for < orphan_threshold:
            continue

        with db._connect() as conn:
            recent = conn.execute(
                "SELECT id FROM watchdog_events "
                "WHERE thread_id=? AND event_type='orphaned' AND created_at > ?",
                (thread_id, dedup_cutoff),
            ).fetchone()
        if recent:
            continue

        label = thread.get("user_label") or thread.get("label") or thread_id[:8]
        mins = int(orphaned_for // 60)
        last_task = thread.get("last_dispatched_task")
        task_snippet = f"\n  Last task: {last_task[:80]}..." if last_task else ""

        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"🔴 [{label}] orphaned — background thread with no agent for {mins} min"
                f"{task_snippet}\n"
                f"  State: orphaned\n"
                f"  Last activity: {mins} min ago\n"
                f"  Recovery attempted: none (auto-recovery OOS v1)\n"
                f"  Next step: re-dispatch manually"
            ),
            type_="failure", priority="high",
        )
        # DA-7: use sentinel agent_id, not empty string
        db.add_watchdog_event(
            agent_id="orphan_detector",
            thread_id=thread_id,
            event_type="orphaned",
            snapshot_path=None,
        )
        orphaned.append(thread_id)
        _log.warning("Watchdog: orphaned thread %s (%s, %d min no agent)",
                     thread_id[:8], label, mins)

    return orphaned


# ---------------------------------------------------------------------------
# inspect_agent — high-level entry point used by active test suite
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    from juggle_settings import get_settings
    return Path(get_settings()["paths"]["config_dir"])


def inspect_agent(agent_id: str, db: Any, tmux_session: str) -> dict:
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
    from datetime import datetime, timezone

    agent = db.get_agent(agent_id)
    if agent is None:
        return {"state": "crashed", "actions": ["agent_missing"],
                "action_item_id": None, "notification_id": None}

    pane_id = agent["pane_id"]
    thread_id = agent.get("assigned_thread")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]
    session_id = get_session_id(db)

    stall_threshold = float(os.environ.get("JUGGLE_WATCHDOG_STALL_SECS", "60"))

    config_dir = _config_dir()
    snapshot_dir = config_dir / "watchdog" / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # Capture pane content
    cap = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id],
        capture_output=True, text=True,
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

    if raw_content is None:
        # Pane gone entirely
        return _handle_crashed(db, agent, thread_id, label, session_id, result,
                                pane_content="", snapshot_dir=snapshot_dir)

    content = _strip_ansi(raw_content)
    _lines = content.splitlines()
    while _lines and not _lines[-1].strip():
        _lines.pop()
    tail = "\n".join(_lines[-15:])

    # 1. Allowlist prompts — check both single-line format and multiline fixture format
    matched_key: str | None = None
    for pattern, key in _ALLOWLIST:
        if pattern in tail:
            matched_key = key
            break
    # Flexible: detect multiline "1. Yes ... 2. Yes ... 3. No" permission dialog
    if matched_key is None and re.search(r"1\.\s+Yes", tail) and re.search(r"2\.\s+Yes", tail):
        matched_key = "2"

    if matched_key is not None:
        result["state"] = "recoverable_prompt"
        result["actions"].append("sent_key")
        if matched_key:
            subprocess.run(["tmux", "send-keys", "-t", pane_id, matched_key, "Enter"],
                           capture_output=True)
        else:
            subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"],
                           capture_output=True)
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
    is_shell_prompt = (
        any(last_nonempty.endswith(suffix) for suffix in _SHELL_SUFFIXES)
        or any(indicator in last_nonempty for indicator in _SHELL_INDICATORS)
    )
    if is_shell_prompt:
        return _handle_crashed(db, agent, thread_id, label, session_id, result,
                                pane_content=content, snapshot_dir=snapshot_dir)

    # 3. ╭─╮ box → stuck_at_prompt (send Enter to unblock)
    if _has_box_top(content):
        result["state"] = "stuck_at_prompt"
        result["actions"].append("sent_enter")
        subprocess.run(["tmux", "send-keys", "-t", pane_id, "Enter"], capture_output=True)
        notif_id = db.add_notification_v2(
            thread_id=thread_id,
            message=f"[Watchdog] [{label}] stuck-at-prompt — sent Enter to unblock",
            session_id=session_id,
        )
        result["notification_id"] = notif_id
        return result

    # 4. Stall detection — compare stall_for against threshold
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
            message=(f"🚨 [{label}] agent stalled (silent for {int(stall_for)}s) — "
                     f"snapshot at {snap_path}"),
            type_="failure", priority="high",
        )
        result["action_item_id"] = item_id
        return result

    # 5. Working
    result["state"] = "working"
    return result


def _handle_crashed(
    db: Any, agent: dict, thread_id: str | None, label: str,
    session_id: str, result: dict, pane_content: str,
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
        type_="failure", priority="high",
    )
    result["action_item_id"] = item_id
    return result
