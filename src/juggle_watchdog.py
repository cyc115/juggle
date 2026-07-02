"""Juggle agent watchdog — agent state classification, recovery, and orchestration.

Design principle
----------------
- Action item (add_action_item) = a user decision or action is required.
  Use only when auto-recovery is exhausted or impossible.
- Notification (add_notification_v2) = FYI / status update.
  Use for transient events the system is already handling automatically.

Split modules (re-exported here for backward-compat):
  juggle_watchdog_restart.py  — git-HEAD stale-code detection (daemon exit)
  juggle_watchdog_inspect.py  — inspect_agent entry point + _handle_crashed
"""

from __future__ import annotations

import hashlib as _hashlib
import logging
import os
import re
import subprocess  # noqa: F401 — patch-target anchor (tests patch juggle_watchdog.subprocess.run)
import time as _time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Re-exports from split modules (keep all juggle_watchdog.X patch targets alive)
# ---------------------------------------------------------------------------
from juggle_watchdog_restart import (  # noqa: E402, F401
    current_code_version,
    should_exit_for_stale_code,
)
from juggle_watchdog_inspect import (  # noqa: E402, F401
    _config_dir,
    _handle_crashed,
    inspect_agent,
)

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

# Context-recycle threshold: alive agents above this fraction get decommissioned
# and replaced with a fresh agent instead of nudged.
# Overridable via env var JUGGLE_AGENT_CONTEXT_RECYCLE_PCT (e.g. "0.75").
_CONTEXT_RECYCLE_THRESHOLD: float = float(
    os.environ.get("JUGGLE_AGENT_CONTEXT_RECYCLE_PCT", "0.80")
)

# Matches the CC pane footer context usage: e.g. "Sonnet 4.6(164.0k/200.0k)"
_CTX_USAGE_RE = re.compile(r"\((\d+(?:\.\d+)?)(k?)/(\d+(?:\.\d+)?)(k?)\)")

# Matches CC thinking spinner: timer pattern "(26s ·" / "(6m 17s ·" or known
# thinking-word synonyms. Timer detection is generic; synonyms are a fallback.
_THINKING_RE = re.compile(
    r"(?:"
    r"\(\d+(?:m \d+)?s[\s\xb7]"  # (26s · or (6m 17s · (U+00B7 middle dot)
    r"|\bThinking\b"
    r"|\b(?:Befuddling|Burrowing|Saut[eé]ed|Cooked|Churned|Brewed|Baked|Crunched?"
    r"|Garnishing|Newspapering|Stewing|Billowing|Sprouting|Warping)\b"
    r")"
)

# Grace period before a never-tasked agent can be decommissioned.
# Overridable via juggle_settings key "agent_boot_grace_secs".
_BOOT_GRACE_SECS: float = 120.0

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


def _parse_context_pct(content: str) -> float | None:
    """Parse context usage fraction from a CC pane footer.

    Matches patterns like 'Sonnet 4.6(164.0k/200.0k)'.
    Returns float in [0, 1], or None if not parseable.
    """
    m = _CTX_USAGE_RE.search(content)
    if not m:
        return None
    used_val, used_k, total_val, total_k = m.groups()
    used = float(used_val) * (1000.0 if used_k else 1.0)
    total = float(total_val) * (1000.0 if total_k else 1.0)
    if total == 0:
        return None
    return used / total


def _has_active_spinner(content: str) -> bool:
    """Return True if content shows a CC active-thinking spinner or timer."""
    return bool(_THINKING_RE.search(content))


def recovery_action(
    *,
    context_pct: float | None,
    has_active_spinner: bool,
    is_dead: bool,
    never_fired: bool,
    context_recycle_threshold: float = _CONTEXT_RECYCLE_THRESHOLD,
) -> str:
    """Pure decision function: given parsed pane signals, return the recovery action.

    Returns one of:
      "respawn" — pane gone or never launched; decommission + spawn fresh
      "none"    — agent is actively working (spinner visible); leave it alone
      "recycle" — alive but context above threshold; decommission + spawn fresh
      "nudge"   — alive, low context; send continue instruction
    """
    if is_dead or never_fired:
        return "respawn"
    if has_active_spinner:
        return "none"
    if context_pct is not None and context_pct >= context_recycle_threshold:
        return "recycle"
    return "nudge"


def _has_box_top(content: str) -> bool:
    return any(_BOX_TOP_RE.match(line) for line in content.splitlines())


# ---------------------------------------------------------------------------
# Snapshot helpers (extracted; re-exported for existing imports)
# ---------------------------------------------------------------------------

from juggle_watchdog_snapshots import (  # noqa: E402, F401
    read_snapshot,
    write_recovery_snapshot,
    write_snapshot,
)


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

    if _has_active_spinner(tail) or stalled_for < 60:
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

    Only meaningful for interactive harnesses (Claude Code) — for non-interactive
    (one-shot) agents, use ``_agent_is_non_interactive`` + ``oneshot_agent_alive``.
    """
    if not pane_exists:
        return "dead"
    if any(marker in pane_content for marker in _CLAUDE_UI_MARKERS):
        return "alive_slow"
    return "never_fired"


def _agent_is_non_interactive(agent: dict) -> bool:
    """Return True if the agent's persisted harness is non-interactive (one-shot).

    Resolves the adapter from the **persisted** harness id (so a recycled claude
    pane still shows as interactive even if current config says reasonix).
    """
    try:
        harness_id = agent.get("harness")
        if not harness_id:
            return False
        # Resolve using the agent's OWN harness config, not the current global default.
        from juggle_settings import get_settings
        agent_cfg = get_settings().get("agent", {})
        harnesses = agent_cfg.get("harnesses") or {}
        hcfg = harnesses.get(harness_id)
        if hcfg is not None:
            # Use the adapter type from config to determine interactivity
            is_interactive = hcfg.get("interactive", True)
            return not is_interactive
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Nudge + notify — for alive-but-slow agents
# ---------------------------------------------------------------------------

_CONTINUE_INSTRUCTION = (
    "You appear paused. Assess your task: if work remains, continue to completion and "
    "do not wait for input; if everything is complete, run your complete-agent command "
    "now; if blocked, call complete-agent with 'BLOCKER: <what>'."
)
# Escalating backoff (seconds) indexed by prior nudge count (clamped at last entry).
_NUDGE_BACKOFF_SECS = [0, 5 * 60, 15 * 60, 30 * 60]
# In-memory per-agent nudge state: {agent_id: (last_fired_time, fire_count)}
_nudge_state: dict[str, tuple[float, int]] = {}


def nudge_and_notify(db: Any, mgr: Any, agent: dict, content: str) -> None:
    """Send a continue-instruction nudge and emit a notification for passive user visibility.

    Uses escalating backoff to avoid spamming on every watchdog cycle.
    Sends Escape + continue instruction + Enter (not a bare Enter) to resume
    an agent paused at a turn boundary.
    Does NOT kill the pane or spawn a replacement, and does NOT file a blocking
    action item.  alive-but-slow is informational.
    """
    from datetime import datetime, timezone

    agent_id = agent["id"]
    pane_id = agent.get("pane_id", "")
    thread_id = agent.get("assigned_thread")
    role = agent.get("role", "researcher")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    # Backoff gate: skip if within the escalating quiet window for this agent.
    last_fired, fire_count = _nudge_state.get(agent_id, (0.0, 0))
    backoff_secs = _NUDGE_BACKOFF_SECS[min(fire_count, len(_NUDGE_BACKOFF_SECS) - 1)]
    now_ts = _time.time()
    if fire_count > 0 and (now_ts - last_fired) < backoff_secs:
        return

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

    # Send Escape to exit any mode, then the continue instruction, then Enter.
    # Mirrors the empirically-proven sequence that unsticks an agent at a turn boundary.
    try:
        mgr._run_tmux("send-keys", "-t", pane_id, "Escape")
        _time.sleep(0.1)
        mgr._run_tmux("send-keys", "-t", pane_id, _CONTINUE_INSTRUCTION)
        mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
    except Exception:
        pass

    _nudge_state[agent_id] = (now_ts, fire_count + 1)

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

    if _agent_is_non_interactive(live):
        # One-shot agent: use PID liveness, not pane markers.
        # Pane markers are meaningless for non-interactive harnesses (no Claude UI).
        from juggle_tmux import oneshot_agent_alive as _oneshot_alive
        if _oneshot_alive(live):
            # Still running — no recovery needed.
            _log.info(
                "Watchdog: non-interactive agent %s is alive (PID check) — skipping",
                agent_id[:8],
            )
            return
        # Dead one-shot + pane still exists but process died → treat as never_fired
        # Fall through to recovery below.
        if not pane_exists:
            agent_state = "dead"
        else:
            # Process died but pane may still show shell — proceed to recovery.
            agent_state = "never_fired"
    else:
        agent_state = _classify_agent_state(pane_content, pane_exists)
        if agent_state == "alive_slow":
            _t = live.get("assigned_thread")
            if _t:
                _thread = db.get_thread(_t)
                if _thread and _thread.get("state") == "done":
                    _log.info(
                        "Watchdog: agent %s alive_slow but thread %s is closed — idling agent",
                        agent_id[:8], _t[:8],
                    )
                    db.update_agent(agent_id, status="idle", assigned_thread=None)
                    return
            ctx_pct = _parse_context_pct(pane_content)
            active = _has_active_spinner(pane_content)
            action = recovery_action(
                context_pct=ctx_pct,
                has_active_spinner=active,
                is_dead=False,
                never_fired=False,
            )
            if action == "recycle":
                _log.warning(
                    "Watchdog: alive_slow at high context (%.0f%%) — recycling to fresh agent",
                    ctx_pct * 100,  # type: ignore[operator]
                )
                # fall through to decommission + re-dispatch below
            elif action == "none":
                _log.info(
                    "Watchdog: alive_slow with active spinner — leaving agent %s alone",
                    agent_id[:8],
                )
                return
            else:
                nudge_and_notify(db, mgr, live, pane_content)
                return

    thread_id = live.get("assigned_thread")
    role = live.get("role", "researcher")
    model = None  # Fix 4: always use current config model; never forward stale snapshot model
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

    # Liveness recheck (Fix 3): re-capture pane content before committing to recovery.
    # If the hash changed since the watchdog's original observation, the agent is still
    # working (e.g. running a long test suite) — abort to avoid duplicate dispatch.
    try:
        _recheck_content = mgr.capture_pane(live["pane_id"])
        if _recheck_content is not None:
            _initial_hash = _hash_tail(_strip_ansi(pane_content))
            _recheck_hash = _hash_tail(_strip_ansi(_recheck_content))
            if _initial_hash != _recheck_hash:
                _log.info(
                    "Watchdog: recovery aborted for %s — pane hash changed (agent still active)",
                    agent_id[:8],
                )
                return
    except Exception as _exc:
        _log.debug("Watchdog: liveness recheck failed for %s: %s — proceeding", agent_id[:8], _exc)

    snap_path = write_recovery_snapshot(agent_id, pane_content, recovery_dir)
    _log.info("Watchdog: recovery snapshot saved to %s", snap_path)

    if thread_id:
        # P8 Task 4.2: update_thread mirrors the conversation node get_thread reads.
        db.update_thread(
            thread_id,
            last_dispatched_task=last_task,
            last_dispatched_role=role,
            last_dispatched_model=model,
        )

    # Kill pane (best-effort) then delete agent from DB directly
    try:
        mgr.kill_pane(live["pane_id"])
    except Exception:
        pass
    db.delete_agent(agent_id)
    try:  # Ledger: a reaped agent's open run must not linger as 'dispatched'.
        db.fail_open_runs(thread_id=thread_id, agent_id=agent_id)
    except Exception:
        _log.warning("Watchdog: ledger fail_open_runs failed for %s", agent_id[:8])

    if thread_id:
        db.update_thread(thread_id, status="failed")

    if live.get("watchdog_retried", 0) >= 1:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=(
                    f"[RQ] [{label}] {role} agent failed 2× (auto-recovery exhausted). "
                    f"Decide: re-dispatch / abandon / investigate. "
                    f"Cause: stalled/crashed again after watchdog retry."
                ),
                type_="failure",
                priority="high",
            )
        if thread_id:
            # Agent death must reach the graph (DA round-2 MAJOR-1,
            # 2026-06-10): bound task → failed-exec + dependents blocked.
            try:
                from juggle_cmd_agents_graph import fail_graph_task

                fail_graph_task(
                    db, thread_id, session_id,
                    reason="watchdog auto-recovery exhausted (failed 2x)",
                )
            except Exception:
                _log.exception(
                    "Watchdog: graph fail-marking failed for thread %s",
                    thread_id[:8],
                )
        db.add_watchdog_event(
            agent_id=agent_id,
            thread_id=thread_id,
            event_type="retry_blocked",
            snapshot_path=str(snap_path),
        )
        return

    if thread_id:
        db.add_notification_v2(
            thread_id=thread_id,
            message=(
                f"[Watchdog] [{label}] {role} stalled/crashed — auto-retrying "
                f"(recovery snapshot: {snap_path.name})"
            ),
            session_id=session_id,
        )

    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    # Fix 3b: if thread was closed DURING spawn (original agent finished just-in-time),
    # release the recovery agent immediately — update_thread below would otherwise
    # overwrite the "closed" status, hiding the completion.
    if thread_id:
        _thread_post_spawn = db.get_thread(thread_id)
        if _thread_post_spawn and _thread_post_spawn.get("state") == "done":
            _log.info(
                "Watchdog: recovery agent %s released — thread %s closed during spawn window",
                new_agent_id[:8], thread_id[:8],
            )
            db.update_agent(new_agent_id, status="idle", assigned_thread=None)
            return

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
        db.set_conversation_background(thread_id)

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
        db.add_notification_v2(
            thread_id=thread_id,
            message=(
                f"[Watchdog] [{label}] {role} agent auto-re-dispatched to "
                f"{new_agent_id[:8]} after stall"
            ),
            session_id=session_id,
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


_ORPHAN_MAX_RECOVERY_ATTEMPTS = 2


def check_orphaned_threads(
    db: Any,
    *,
    orphan_threshold: float = 300.0,
    dedup_window_hours: float = 24.0,
    mgr: Any = None,
    max_recovery_attempts: int = _ORPHAN_MAX_RECOVERY_ATTEMPTS,
) -> list[str]:
    """Scan background threads with no active agent; auto-recover or file action items.

    Returns list of orphaned thread_ids detected this cycle. Uses 24h dedup guard.
    When mgr is provided and last_dispatched_task exists, auto-recovers by re-dispatching
    the last task to a fresh agent (reusing execute_recovery spawn path).
    Falls back to manual action item if: no mgr, no task, pool full, or max attempts reached.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    dedup_cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    with db._connect() as conn:
        thread_rows = conn.execute(  # P8 Task 3.1 (R2-1): read background from nodes
            "SELECT * FROM nodes WHERE kind='conversation' AND state='background'"
        ).fetchall()
        threads = [dict(r) for r in thread_rows]
        busy_rows = conn.execute(
            "SELECT assigned_thread FROM agents WHERE status='busy' AND assigned_thread IS NOT NULL"
        ).fetchall()
        busy_thread_ids = {r["assigned_thread"] for r in busy_rows}
        busy_count = conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status='busy'"
        ).fetchone()[0]

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
        role = thread.get("last_dispatched_role")  # None = unknown; auto-recovery skipped
        # Fix 4 (mirrors execute_recovery): never forward the stale snapshot
        # model — always let spawn_agent re-resolve from current config
        # (2026-07-01 coder model config ignored).
        model = None

        # Attempt auto-recovery when possible
        did_recover = False
        if mgr is not None and last_task:
            with db._connect() as conn:
                attempt_count = conn.execute(
                    "SELECT COUNT(*) FROM watchdog_events "
                    "WHERE thread_id=? AND event_type='orphan_recovery'",
                    (thread_id,),
                ).fetchone()[0]

            try:
                from juggle_settings import resolve_max_agents
                max_agents = resolve_max_agents()
            except Exception:
                max_agents = 20

            pool_full = busy_count >= max_agents

            if attempt_count < max_recovery_attempts and not pool_full and role:
                try:
                    new_agent = mgr.spawn_agent(db, role=role, model=model)
                    new_agent_id = new_agent["id"]
                    new_pane_id = new_agent["pane_id"]
                    ts = now.isoformat()
                    db.update_agent(
                        new_agent_id,
                        status="busy",
                        assigned_thread=thread_id,
                        last_active=ts,
                        busy_since=ts,
                        last_task=last_task,
                    )
                    db.set_conversation_background(thread_id)
                    mgr.send_task(new_pane_id, last_task)
                    db.add_watchdog_event(
                        agent_id="orphan_detector",
                        thread_id=thread_id,
                        event_type="orphan_recovery",
                        snapshot_path=None,
                    )
                    _sid = ""
                    try:
                        _sid = get_session_id(db)
                    except Exception:
                        pass
                    db.add_notification_v2(
                        thread_id=thread_id,
                        message=(
                            f"[Watchdog] [{label}] orphaned thread auto-recovery: "
                            f"re-dispatched to agent {new_agent_id[:8]} "
                            f"(attempt {attempt_count + 1}/{max_recovery_attempts}, "
                            f"{mins} min no agent)"
                        ),
                        session_id=_sid,
                    )
                    did_recover = True
                    _log.info(
                        "Watchdog: orphan auto-recovery — thread %s re-dispatched to agent %s (attempt %d)",
                        thread_id[:8],
                        new_agent_id[:8],
                        attempt_count + 1,
                    )
                except Exception as exc:
                    _log.error(
                        "Watchdog: orphan auto-recovery failed for thread %s: %s",
                        thread_id[:8],
                        exc,
                    )

        if not did_recover:
            task_snippet = f" Last task: {last_task[:80]}..." if last_task else ""
            db.add_action_item(
                thread_id=thread_id,
                message=(
                    f"[RQ] [{label}] orphaned thread — auto-recovery exhausted. "
                    f"Decide: re-dispatch / abandon / investigate. "
                    f"Cause: background thread with no agent for {mins} min.{task_snippet}"
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
# Singleton helpers (used by daemon entry point, tested independently)
# ---------------------------------------------------------------------------


def _is_watchdog_process(pid: int) -> bool:
    """Return True if the process with given PID is a juggle watchdog."""
    import daemon_pidfile

    return daemon_pidfile.is_process(pid, "watchdog", case_insensitive=True)


def _kill_existing_watchdog_from_pidfile(pidfile_path: Path) -> None:
    """Kill the watchdog recorded in pidfile_path — only if it really is a watchdog.

    Thin shim over daemon_pidfile.kill_existing_from_pidfile (single source of
    truth). The predicate is looked up via module globals at call time so tests
    monkeypatching juggle_watchdog._is_watchdog_process keep working.
    """
    import daemon_pidfile

    daemon_pidfile.kill_existing_from_pidfile(
        pidfile_path,
        lambda pid: _is_watchdog_process(pid),
        log=_log,
        name="watchdog",
    )


