"""juggle_watchdog_stall — nudge busy agents idling at the prompt.

Problem (user-approved 2026-07-01, Option B): coder agents finish, print a recap,
and idle at the harness READY prompt without finalizing (integrate + agent
complete). This detector runs each watchdog tick, for every busy agent:

  * STALL SIGNAL = the READY prompt is drawn (a readiness_marker visible) AND no
    activity indicator (no submission_marker: spinner / 'esc to interrupt').
    Markers come from the harness adapter — the SSOT, never hardcoded here.
  * DEBOUNCE: counts as stalled only after >= stall_threshold_minutes idle;
    first-seen-idle is tracked per agent and cleared on any activity.
  * ACTION: auto-send a finalize nudge via the existing steering-message path.
  * ESCALATION: after max_stall_nudges with no state change, file ONE HIGH action
    item and stop nudging (never kill the pane). Counter resets on re-dispatch.

Self-contained by design: the planned spool single-writer redesign removes the
agent-side integrate step, but "pane idle while the agent is busy" stays a
meaningful signal, so this detector survives it.
"""
from __future__ import annotations

import logging
import re
import time as _time
from typing import Any, Callable

_log = logging.getLogger("juggle-watchdog")

# Config defaults (overridable under the ``watchdog`` settings block).
_DEFAULT_STALL_THRESHOLD_MIN = 3.0
_DEFAULT_MAX_STALL_NUDGES = 2

NUDGE_TEXT = (
    "You appear stopped at the prompt mid-task. Continue and finalize "
    "(integrate + agent complete), or agent complete with a BLOCKER."
)


# Pure detector


def is_idle_at_prompt(
    content: str | None,
    readiness_markers: tuple,
    submission_markers: tuple,
    active_pattern: str = "",
) -> bool:
    """True iff the pane shows the READY prompt with no activity indicator.

    A submission marker (spinner / 'esc to interrupt') means the agent is working
    → never a stall. Empty/None content is never a stall. ``active_pattern``
    (SSOT: adapter's ``active_status_pattern``) is an additional structural
    signal — survives unenumerated glyphs (2026-07-02 false-positive on ZJ)."""
    if not content:
        return False
    if any(m in content for m in submission_markers):
        return False
    if active_pattern and re.search(active_pattern, content):
        return False
    return any(m in content for m in readiness_markers)

# Per-agent debounce / nudge / escalation state machine


class StallTracker:
    """Cross-tick state for the stall detector (one instance per daemon).

    ``decide`` does no IO: it reads/writes in-memory maps and returns the action
    the driver should take. State survives ticks and resets per agent when its
    dispatch identity changes.
    """

    def __init__(self) -> None:
        self.idle_since: dict[str, float] = {}
        self.nudges: dict[str, int] = {}
        self.escalated: set[str] = set()
        self.dispatch_key: dict[str, str] = {}

    def _reset(self, agent_id: str) -> None:
        self.idle_since.pop(agent_id, None)
        self.nudges.pop(agent_id, None)
        self.escalated.discard(agent_id)

    def forget(self, agent_id: str) -> None:
        """Drop all state for an agent (e.g. it is no longer busy)."""
        self._reset(agent_id)
        self.dispatch_key.pop(agent_id, None)

    def decide(
        self,
        agent_id: str,
        *,
        idle: bool,
        now: float,
        threshold_s: float,
        max_nudges: int,
        dispatch_key: str,
    ) -> str:
        """Advance one tick. Returns: 'active' (not idle; debounce cleared),
        'waiting' (idle, window not elapsed), 'nudge' (threshold reached; counter
        bumped), 'escalate' (max nudges exhausted; file HIGH item once), or
        'silent' (already escalated)."""
        # New dispatch / agent reuse → fresh state.
        if self.dispatch_key.get(agent_id) != dispatch_key:
            self.dispatch_key[agent_id] = dispatch_key
            self._reset(agent_id)

        if not idle:
            self.idle_since.pop(agent_id, None)
            return "active"

        first = self.idle_since.get(agent_id)
        if first is None:
            self.idle_since[agent_id] = now
            return "waiting"
        if now - first < threshold_s:
            return "waiting"

        if agent_id in self.escalated:
            return "silent"

        n = self.nudges.get(agent_id, 0)
        if n >= max_nudges:
            self.escalated.add(agent_id)
            return "escalate"

        self.nudges[agent_id] = n + 1
        self.idle_since[agent_id] = now  # re-arm the debounce → space out nudges
        return "nudge"


# Config + adapter resolution


def _stall_config() -> tuple[float, int]:
    """Return (stall_threshold_minutes, max_stall_nudges) from settings."""
    try:
        from juggle_settings import get_settings
        wd = get_settings().get("watchdog", {}) or {}
    except Exception:
        wd = {}
    thr = float(wd.get("stall_threshold_minutes", _DEFAULT_STALL_THRESHOLD_MIN))
    maxn = int(wd.get("max_stall_nudges", _DEFAULT_MAX_STALL_NUDGES))
    return thr, maxn


def markers_for_agent(agent: dict) -> tuple[tuple, tuple]:
    """Resolve (readiness, submission) markers for an agent from the harness
    adapter (SSOT, never hardcoded) — by the agent's role via ``get_adapter``."""
    try:
        from juggle_harness import get_adapter
        adapter = get_adapter(role=agent.get("role"))
        return adapter.readiness_markers(), adapter.submission_markers()
    except Exception:
        return (), ()

def _capture_pane(mgr: Any, pane_id: str, lines: int = 80) -> str | None:
    """Capture pane text (mirrors the daemon helper; returns None if pane gone)."""
    try:
        if not mgr.verify_pane(pane_id):
            return None
        result = mgr._run_tmux("capture-pane", "-pt", pane_id, "-S", f"-{lines}")
        if getattr(result, "returncode", 1) != 0:
            return None
        return result.stdout or ""
    except Exception:
        return None

def _dispatch_key(agent: dict) -> str:
    """Identity of the agent's current dispatch — changes on re-dispatch/reuse."""
    return f"{agent.get('assigned_thread')}|{agent.get('busy_since')}|{agent.get('last_task')}"

def _thread_label(db: Any, thread_id: str | None, agent_id: str) -> str:
    if not thread_id:
        return agent_id[:8]
    try:
        thread = db.get_thread(thread_id)
    except Exception:
        thread = None
    if not thread:
        return thread_id[:8]
    return thread.get("user_label") or thread.get("label") or thread_id[:8]

# Driver — the watchdog tick entry point


def check_stalled_agents(
    db: Any,
    mgr: Any,
    tracker: StallTracker,
    *,
    now: float | None = None,
    session_id: str = "",
    capture: Callable[[str], str | None] | None = None,
    markers_for: Callable[[dict], tuple[tuple, tuple]] | None = None,
    active_pattern_for: Callable[[dict], str] | None = None,
) -> None:
    """Detect busy agents idling at the prompt and nudge / escalate them.
    ``capture``/``markers_for``/``active_pattern_for`` are injectable for tests."""
    if now is None:
        now = _time.time()
    if capture is None:
        capture = lambda pid: _capture_pane(mgr, pid)  # noqa: E731
    if markers_for is None:
        markers_for = markers_for_agent
    if active_pattern_for is None:
        from juggle_harness import active_pattern_for_agent as active_pattern_for

    threshold_min, max_nudges = _stall_config()
    threshold_s = threshold_min * 60.0

    # Only interactive agents have a prompt to idle at; one-shot agents never do.
    from juggle_watchdog import _agent_is_non_interactive
    for agent in db.get_all_agents():
        if agent.get("status") != "busy":
            continue
        agent_id = agent["id"]
        if _agent_is_non_interactive(agent):
            tracker.forget(agent_id)
            continue

        pane_id = agent.get("pane_id") or ""
        content = capture(pane_id)
        ready, submit = markers_for(agent)
        idle = is_idle_at_prompt(content, ready, submit, active_pattern_for(agent))

        action = tracker.decide(
            agent_id,
            idle=idle,
            now=now,
            threshold_s=threshold_s,
            max_nudges=max_nudges,
            dispatch_key=_dispatch_key(agent),
        )

        if action == "nudge":
            _send_nudge(db, mgr, agent, tracker.nudges[agent_id], max_nudges, session_id)
        elif action == "escalate":
            _escalate(db, agent, max_nudges, session_id)


def _send_nudge(
    db: Any, mgr: Any, agent: dict, nudge_n: int, max_nudges: int, session_id: str
) -> None:
    agent_id = agent["id"]
    pane_id = agent.get("pane_id") or ""
    thread_id = agent.get("assigned_thread")
    label = _thread_label(db, thread_id, agent_id)

    try:
        mgr.send_message(pane_id, NUDGE_TEXT)
    except Exception as exc:
        _log.warning(
            "Watchdog stall: nudge send failed for agent %s on pane %s: %s",
            agent_id[:8], pane_id, exc,
        )
        return

    _log.info(
        "Watchdog stall: nudged agent %s [%s] idling at prompt (nudge %d/%d)",
        agent_id[:8], label, nudge_n, max_nudges,
    )
    if thread_id:
        try:
            db.add_notification_v2(
                thread_id=thread_id,
                message=(
                    f"[Watchdog] [{label}] idle-at-prompt mid-task — sent finalize "
                    f"nudge ({nudge_n}/{max_nudges})"
                ),
                session_id=session_id,
            )
        except Exception:
            pass


def _escalate(db: Any, agent: dict, max_nudges: int, session_id: str) -> None:
    agent_id = agent["id"]
    thread_id = agent.get("assigned_thread")
    role = agent.get("role", "agent")
    label = _thread_label(db, thread_id, agent_id)

    _log.warning(
        "Watchdog stall: agent %s [%s] still idle at prompt after %d nudges — "
        "filing HIGH action item, stopping nudges",
        agent_id[:8], label, max_nudges,
    )
    try:
        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"[RQ] [{label}] {role} agent idle at the prompt mid-task after "
                f"{max_nudges} finalize nudges — no state change. Decide: steer / "
                f"complete with BLOCKER / investigate."
            ),
            type_="failure",
            priority="high",
        )
    except Exception:
        _log.exception("Watchdog stall: failed to file action item for %s", agent_id[:8])
