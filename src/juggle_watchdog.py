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
# Hard floor per role so the adaptive 2*median threshold can't collapse
# below legitimate long-command runtime (e.g. coders running test suites).
_MIN_STALL_THRESHOLD_SECS: dict[str, float] = {
    "coder": 600.0,      # 10 min — coders run test suites
    "planner": 300.0,    # 5 min
    "researcher": 180.0, # 3 min
}
_MIN_STALL_FALLBACK_SECS = 180.0
_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")
_CLAUDE_UI_MARKERS = (
    "Welcome",
    "Bypass permissions",
    "INSERT",
    "Cogitated",
    "Working",
    "shortcuts",
    "claude.ai/code",
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_TOP_RE = re.compile(r"^╭─+╮\s*$")
# Sentinel: caller did not supply last_send_task_at, so backward-compat applies.
_NO_DISPATCH_INFO: object = object()

# Grace period before a never-tasked agent can be decommissioned.
# Overridable via juggle_settings key "agent_boot_grace_secs".
_BOOT_GRACE_SECS: float = 120.0

# ---------------------------------------------------------------------------
# Stale-code detection (pure helper — used by daemon process)
# ---------------------------------------------------------------------------


def _is_source_stale(recorded_mtime: float, source_path: Path) -> bool:
    """Return True if source_path has been modified since recorded_mtime."""
    try:
        return source_path.stat().st_mtime > recorded_mtime
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Hot-restart on source change
# ---------------------------------------------------------------------------

_HOT_RESTART_GRACE_SECS: float = 300.0  # files must be stable this long before re-exec


def should_hot_restart(
    baseline_mtimes: dict[str, float],
    current_mtimes: dict[str, float],
    last_change_at: float | None,
    now: float,
    grace_secs: float = _HOT_RESTART_GRACE_SECS,
) -> tuple[bool, float | None]:
    """Pure decision function for hot-restart.

    Returns (ready_to_restart, new_last_change_at).

    Stability-window logic: a change must be stable (no further mtime shifts)
    for >= grace_secs before restart is authorised, preventing restarts on
    half-written files or edit/save flurries.
    """
    changed = current_mtimes != baseline_mtimes

    if not changed:
        # No change, or files reverted to original baseline — cancel pending restart.
        return False, None

    if last_change_at is None:
        # First detection this cycle — record timestamp; not ready yet.
        return False, now

    if now - last_change_at >= grace_secs:
        return True, last_change_at

    return False, last_change_at


def _collect_mtimes(src_dir: Path, entry_script: Path | None = None) -> dict[str, float]:
    """Stat all src/*.py files plus the optional entry script; return {str(path): mtime}."""
    paths: list[Path] = sorted(src_dir.glob("*.py"))
    if entry_script and entry_script.exists():
        paths.append(entry_script)
    result: dict[str, float] = {}
    for p in paths:
        try:
            result[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return result


def _maybe_hot_restart(
    baseline_mtimes: dict[str, float],
    state: dict,
    src_dir: Path,
    entry_script: Path | None = None,
) -> None:
    """Thin wrapper: stat files, call should_hot_restart, and re-exec if ready.

    ``state`` is a mutable dict with keys:
      - ``last_change_at``: float | None
      - ``prev_current_mtimes``: dict[str, float]

    The wrapper resets ``last_change_at`` when it detects the current mtimes
    have shifted since the previous poll (further edit after first detection).
    """
    import sys as _sys

    now = _time.time()
    current = _collect_mtimes(src_dir, entry_script)

    # If mtimes have changed further since the last poll, reset the timer so
    # the grace period restarts from this moment.
    prev = state.get("prev_current_mtimes", {})
    if prev and current != prev and current != baseline_mtimes:
        state["last_change_at"] = None

    state["prev_current_mtimes"] = current

    ready, new_lca = should_hot_restart(
        baseline_mtimes, current, state.get("last_change_at"), now
    )
    state["last_change_at"] = new_lca

    if not ready:
        return

    # Crash-guard: verify new code imports cleanly before re-exec'ing.
    check = subprocess.run(
        [_sys.executable, "-c", "import juggle_watchdog"],
        cwd=str(src_dir),
        capture_output=True,
    )
    if check.returncode != 0:
        _log.warning(
            "hot-restart deferred: new code fails to import: %s",
            check.stderr.decode(errors="replace").strip(),
        )
        return

    _log.info("hot-restart: source changed, re-exec'ing")
    os.execv(_sys.executable, [_sys.executable, *_sys.argv])


# ---------------------------------------------------------------------------
# Cold-start cascade dedup state
# ---------------------------------------------------------------------------

_CASCADE_WINDOW_SECS: float = 300.0  # 5-minute sliding window
_CASCADE_THRESHOLD: int = 3  # ≥3 failures → cascade item

_cold_start_failures: dict[str, list[float]] = {}  # thread_id → [timestamps]
_cascade_filed: set[str] = set()  # threads with cascade item already filed


def _record_cold_start_failure(
    thread_id: str | None, *, _now: float | None = None
) -> str:
    """Record a cold-start failure for thread_id.

    Returns one of:
      'skip'             — no thread_id, do nothing
      'normal'           — below threshold, file individual item
      'cascade_fire'     — threshold just hit, file one cascade item
      'cascade_suppress' — cascade already filed, suppress this item
    """
    if not thread_id:
        return "skip"
    ts = _now if _now is not None else _time.time()
    cutoff = ts - _CASCADE_WINDOW_SECS
    failures = [t for t in _cold_start_failures.get(thread_id, []) if t > cutoff]
    failures.append(ts)
    _cold_start_failures[thread_id] = failures
    # Window cleared — reset cascade state so a fresh run can re-fire
    if thread_id in _cascade_filed and len(failures) == 1:
        _cascade_filed.discard(thread_id)
    if thread_id in _cascade_filed:
        return "cascade_suppress"
    if len(failures) >= _CASCADE_THRESHOLD:
        _cascade_filed.add(thread_id)
        return "cascade_fire"
    return "normal"


def _clear_cold_start_failures(thread_id: str | None) -> None:
    """Clear cascade state after successful recovery for thread_id."""
    if thread_id:
        _cold_start_failures.pop(thread_id, None)
        _cascade_filed.discard(thread_id)


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
    agent_snaps = sorted(
        recovery_dir.glob(f"{agent_id}-*.txt"), key=lambda p: p.stat().st_mtime
    )
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
    last_send_task_at: object = _NO_DISPATCH_INFO,
) -> tuple[str, str | None]:
    """Classify agent pane state. Returns (state, key_to_send).

    States: working | crashed | prompt | stuck | quiet | stalled | awaiting_dispatch
    Classification order (most specific first).

    Pass ``last_send_task_at=None`` to signal the agent has never been dispatched;
    the function returns ``awaiting_dispatch`` instead of ``stalled`` so the
    watchdog does not recover agents the orchestrator hasn't sent a task to yet.
    When ``last_send_task_at`` is omitted the old behaviour is preserved.
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

    if last_send_task_at is not _NO_DISPATCH_INFO and last_send_task_at is None:
        return "awaiting_dispatch", None

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
    floor = _MIN_STALL_THRESHOLD_SECS.get(role, _MIN_STALL_FALLBACK_SECS)
    median = db.get_median_duration_secs(role)
    if median is not None:
        return max(2.0 * median, floor)
    return max(_COLD_START_DEFAULTS.get(role, 180.0), floor)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------


def get_session_id(db: Any) -> str:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT value FROM session WHERE key='session_id'"
        ).fetchone()
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
# Agent state classifier for watchdog policy (kill vs nudge decision)
# ---------------------------------------------------------------------------


def _classify_agent_state(pane_content: str, pane_exists: bool) -> str:
    """Classify agent state to decide watchdog action.

    Returns one of: "alive_slow" | "dead" | "never_fired"
    - alive_slow: Claude UI is visible — agent is thinking/working/finished but still alive
    - dead: pane no longer exists in tmux
    - never_fired: pane exists but shows shell with no Claude UI (truncated launch, crash, etc.)
    """
    if not pane_exists:
        return "dead"
    if any(marker in pane_content for marker in _CLAUDE_UI_MARKERS):
        return "alive_slow"
    return "never_fired"


# ---------------------------------------------------------------------------
# Nudge + notify — for alive-but-slow agents
# ---------------------------------------------------------------------------


def nudge_and_notify(db: Any, mgr: Any, agent: dict, content: str) -> None:
    """Send a harmless Enter nudge and emit a notification for passive user visibility.

    Does NOT kill the pane or spawn a replacement, and does NOT file a blocking
    action item.  alive-but-slow is informational — it surfaces as a notification
    so the user can glance at it without being forced to act.
    """
    from datetime import datetime, timezone

    agent_id = agent["id"]
    pane_id = agent.get("pane_id", "")
    thread_id = agent.get("assigned_thread")
    role = agent.get("role", "researcher")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    last_active_str = agent.get("last_active") or agent.get("last_active_at")
    stalled_for = 0
    if last_active_str:
        try:
            last_dt = datetime.fromisoformat(last_active_str.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            stalled_for = int((datetime.now(timezone.utc) - last_dt).total_seconds())
        except (ValueError, TypeError):
            pass

    # Literal Enter — may unstick a permission prompt; harmless otherwise
    try:
        mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
    except Exception:
        pass

    tail_lines = "\n".join(content.splitlines()[-5:]) if content else "(no content)"
    message = (
        f"⚠️ [{label}] [{role}] alive-but-stalled on pane {pane_id}, "
        f"stalled_for={stalled_for}s. Last pane tail:\n{tail_lines}"
    )

    # Derive session_id the same way cmd_notify does.
    session_id = ""
    try:
        with db._connect() as conn:
            srow = conn.execute(
                "SELECT value FROM session WHERE key = 'session_id'"
            ).fetchone()
        if srow:
            session_id = srow["value"] or ""
    except Exception:
        pass

    # Send a passive notification — alive-but-slow is informational, not a blocker.
    if thread_id:
        db.add_notification_v2(
            thread_id=thread_id, message=message, session_id=session_id
        )

    _log.info(
        "Watchdog: nudged + notified agent %s on thread %s (stalled_for=%ds); not killing",
        agent_id[:8],
        label,
        stalled_for,
    )


# ---------------------------------------------------------------------------
# Age helper
# ---------------------------------------------------------------------------


def _get_agent_age_secs(agent: dict) -> float:
    """Return age of the agent in seconds using created_at, falling back to last_active."""
    from datetime import datetime, timezone

    for key in ("created_at", "last_active"):
        ts_str = agent.get(key)
        if ts_str:
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
            except (ValueError, TypeError):
                continue
    return float("inf")  # unknown age → treat as old (safe to decommission)


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
        _log.info(
            "Watchdog: recovery aborted for %s — agent no longer busy", agent_id[:8]
        )
        return

    # Policy: never kill a live agent. Only recover dead / never-fired panes.
    pane_exists = mgr.verify_pane(live["pane_id"])
    agent_state = _classify_agent_state(pane_content, pane_exists)
    if agent_state == "alive_slow":
        nudge_and_notify(db, mgr, live, pane_content)
        return

    thread_id = live.get("assigned_thread")
    role = live.get("role", "researcher")
    model = live.get("model")
    last_task = live.get("last_task")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    # Never-tasked agent: silently decommission — no snapshot, no thread=failed,
    # no action item.  The orchestrator hadn't sent work yet so this is not a
    # real failure.  Guard: skip decommission during cold-boot grace period so
    # freshly-spawned agents aren't reaped before Claude UI has rendered.
    if not last_task:
        try:
            from juggle_settings import get_settings as _get_settings
            _grace = float(_get_settings().get("agent_boot_grace_secs", _BOOT_GRACE_SECS))
        except Exception:
            _grace = _BOOT_GRACE_SECS
        _age = _get_agent_age_secs(live)
        if _age < _grace:
            _log.info(
                "Watchdog: agent %s never-tasked but young (age=%.0fs < grace=%.0fs) — skipping",
                agent_id[:8], _age, _grace,
            )
            return
        _log.info(
            "Watchdog: agent %s never tasked (age=%.0fs >= grace=%.0fs) — silently decommissioning",
            agent_id[:8], _age, _grace,
        )
        try:
            mgr.kill_pane(live["pane_id"])
        except Exception:
            pass
        db.delete_agent(agent_id)
        db.add_watchdog_event(
            agent_id=agent_id,
            thread_id=thread_id,
            event_type="decommissioned_untasked",
            snapshot_path=None,
        )
        return

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
                message=(
                    f"🛑 [{label}] agent stalled AGAIN after watchdog retry — "
                    f"manual intervention required. Snapshot: {snap_path}"
                ),
                type_="failure",
                priority="high",
            )
        db.add_watchdog_event(
            agent_id=agent_id,
            thread_id=thread_id,
            event_type="retry_blocked",
            snapshot_path=str(snap_path),
        )
        return

    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"🚨 [{label}] agent stalled/crashed — snapshot at {snap_path}, "
                f"auto-retrying"
            ),
            type_="failure",
            priority="high",
        )

    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        new_agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_active=now,
        busy_since=now,
        watchdog_retried=1,
        last_task=last_task,
    )
    if thread_id:
        db.update_thread(thread_id, status="background")

    try:
        mgr.send_task(new_pane_id, last_task)
    except RuntimeError as exc:
        _log.error(
            "Watchdog: [RECOVERY-COLD-START-FAILED] send_task raised for agent %s: %s",
            new_agent_id[:8],
            exc,
        )
        # Rollback: thread is not actually being recovered if the agent can't start.
        if thread_id:
            db.update_thread(thread_id, status="failed")
        db.delete_agent(new_agent_id)
        try:
            mgr.kill_pane(new_pane_id)
        except Exception:
            pass
        cascade_status = _record_cold_start_failure(thread_id)
        if thread_id and cascade_status != "cascade_suppress":
            if cascade_status == "cascade_fire":
                db.add_action_item(
                    thread_id=thread_id,
                    message=(
                        f"🛑 [{label}] WATCHDOG-CASCADE-DETECTED — "
                        f"≥{_CASCADE_THRESHOLD} cold-start failures within "
                        f"{int(_CASCADE_WINDOW_SECS // 60)} min. "
                        f"Check spawn config (tmux truncation?). "
                        f"Latest: {exc}"
                    ),
                    type_="failure",
                    priority="high",
                )
            else:
                db.add_action_item(
                    thread_id=thread_id,
                    message=(
                        f"🚨 [{label}] [RECOVERY-COLD-START-FAILED] recovery send_task "
                        f"raised: {exc}. New agent {new_agent_id[:8]} spawned but task "
                        f"not sent — re-dispatch manually."
                    ),
                    type_="failure",
                    priority="high",
                )
        db.add_watchdog_event(
            agent_id=new_agent_id,
            thread_id=thread_id,
            event_type="cold_start_failed",
            snapshot_path=str(snap_path),
        )
        return

    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"⚠️ [{label}] agent auto-re-dispatched after stall — "
                f"verify result when complete"
            ),
            type_="manual_step",
            priority="normal",
        )

    db.add_watchdog_event(
        agent_id=agent_id,
        thread_id=thread_id,
        event_type="recovered",
        snapshot_path=str(snap_path),
    )
    # Clear cascade state and dismiss any open cold-start failure items for this thread
    _clear_cold_start_failures(thread_id)
    if thread_id:
        for item in db.get_open_action_items():
            if item.get("thread_id") == thread_id and "COLD-START-FAILED" in item.get(
                "message", ""
            ):
                db.dismiss_action_item(item["id"])
    _log.info(
        "Watchdog: re-dispatched %s → %s for thread %s",
        agent_id[:8],
        new_agent_id[:8],
        (thread_id or "")[:8],
    )


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
            type_="failure",
            priority="high",
        )
        # DA-7: use sentinel agent_id, not empty string
        db.add_watchdog_event(
            agent_id="orphan_detector",
            thread_id=thread_id,
            event_type="orphaned",
            snapshot_path=None,
        )
        orphaned.append(thread_id)
        _log.warning(
            "Watchdog: orphaned thread %s (%s, %d min no agent)",
            thread_id[:8],
            label,
            mins,
        )

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
        return {
            "state": "crashed",
            "actions": ["agent_missing"],
            "action_item_id": None,
            "notification_id": None,
        }

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
        last_nonempty.endswith(suffix) for suffix in _SHELL_SUFFIXES
    ) or any(indicator in last_nonempty for indicator in _SHELL_INDICATORS)
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
    if _has_box_top(content):
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
