"""Tests for agent context-bloat stall fix (2026-06-15).

Incident: alive_slow agents at high context were nudged instead of recycled,
causing a loop: nudge → auto-compact → stall → nudge → repeat.

Covers:
  1. _parse_context_pct — parses CC footer context usage
  2. recovery_action — pure decision function (no side effects)
  3. _has_active_spinner — thinking synonym + timer detection
  4. classify_pane_state — returns 'quiet' for CC thinking synonyms
  5. execute_recovery — recycles high-context alive agents; nudges low-context
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


@pytest.fixture(autouse=True)
def reset_nudge_state():
    """Clear per-agent backoff state between tests."""
    import juggle_watchdog
    juggle_watchdog._nudge_state.clear()
    yield
    juggle_watchdog._nudge_state.clear()


# ---------------------------------------------------------------------------
# 1. _parse_context_pct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,expected",
    [
        # Standard CC footer format
        (
            "Sonnet 4.6(164.0k/200.0k) | 1% until auto-compact",
            pytest.approx(0.82, abs=0.01),
        ),
        ("Sonnet 4.6(142.0k/200.0k)", pytest.approx(0.71, abs=0.01)),
        ("Sonnet 4.6(0/200.0k)", 0.0),
        ("Claude(200.0k/200.0k)", pytest.approx(1.0, abs=0.01)),
        # Unparseable → None (no regression fallback)
        ("no context info here", None),
        ("", None),
        ("⏵⏵ bypass permissions on (shift+tab to cycle)", None),
    ],
)
def test_parse_context_pct(content, expected):
    from juggle_watchdog import _parse_context_pct

    result = _parse_context_pct(content)
    if expected is None:
        assert result is None
    else:
        assert result == expected


# ---------------------------------------------------------------------------
# 2. recovery_action — pure decision table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        # High context + alive + no spinner → recycle
        (
            {
                "context_pct": 0.85,
                "has_active_spinner": False,
                "is_dead": False,
                "never_fired": False,
            },
            "recycle",
        ),
        # Exactly at default threshold (0.80) → recycle
        (
            {
                "context_pct": 0.80,
                "has_active_spinner": False,
                "is_dead": False,
                "never_fired": False,
            },
            "recycle",
        ),
        # Below threshold → nudge
        (
            {
                "context_pct": 0.40,
                "has_active_spinner": False,
                "is_dead": False,
                "never_fired": False,
            },
            "nudge",
        ),
        # Active spinner → none (don't interrupt, even at high context)
        (
            {
                "context_pct": 0.85,
                "has_active_spinner": True,
                "is_dead": False,
                "never_fired": False,
            },
            "none",
        ),
        (
            {
                "context_pct": 0.40,
                "has_active_spinner": True,
                "is_dead": False,
                "never_fired": False,
            },
            "none",
        ),
        # Dead pane → respawn
        (
            {
                "context_pct": None,
                "has_active_spinner": False,
                "is_dead": True,
                "never_fired": False,
            },
            "respawn",
        ),
        # Never fired → respawn
        (
            {
                "context_pct": None,
                "has_active_spinner": False,
                "is_dead": False,
                "never_fired": True,
            },
            "respawn",
        ),
        # No context info, alive, no spinner → nudge (no regression)
        (
            {
                "context_pct": None,
                "has_active_spinner": False,
                "is_dead": False,
                "never_fired": False,
            },
            "nudge",
        ),
    ],
)
def test_recovery_action(kwargs, expected):
    from juggle_watchdog import recovery_action

    assert recovery_action(**kwargs) == expected


# ---------------------------------------------------------------------------
# 3. _has_active_spinner — thinking synonym + timer detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "✶ Garnishing… (42s · ↓ 1234 tokens)",
        "✶ Newspapering… (42s · ↓ 1234 tokens)",
        "✶ Befuddling… (26s · ↓ 467 tokens · thought for 1s)",
        "✶ Burrowing… (6m 17s · ↓ 12.1k tokens)",
        "✶ Stewing… (5s ·",
        "✶ Billowing… (30s · ↓ 500 tokens)",
        "Thinking",  # original keyword still works
        "✶ Thinking… (10s ·",
    ],
)
def test_has_active_spinner_detects_thinking(snippet):
    """CC spinner/thinking turn must be detected as active."""
    from juggle_watchdog import _has_active_spinner

    assert _has_active_spinner(snippet) is True


@pytest.mark.parametrize(
    "snippet",
    [
        "⏵⏵ bypass permissions on (shift+tab to cycle)\n❯ ",
        "❯ ",
        "",
        "some completed output\n✓ Done",
        "(shift+tab to cycle)",  # parens with non-digit content
    ],
)
def test_has_active_spinner_idle_not_detected(snippet):
    """Idle/done pane must NOT be detected as active spinner."""
    from juggle_watchdog import _has_active_spinner

    assert _has_active_spinner(snippet) is False


# ---------------------------------------------------------------------------
# 4. classify_pane_state — quiet for CC thinking synonyms (not stalled)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "thinking_word",
    ["Garnishing", "Newspapering", "Befuddling", "Burrowing", "Stewing", "Billowing"],
)
def test_classify_pane_state_quiet_for_thinking_synonym(thinking_word):
    """classify_pane_state must return 'quiet' when pane shows a CC thinking synonym.

    Incident 2026-06-15: 'Befuddling'/'Burrowing' not in _EXECUTION_MARKERS caused
    legit thinking turns to be misclassified as stalled, triggering disruptive nudges.
    """
    from juggle_watchdog import classify_pane_state

    content = (
        f"previous output\n"
        f"✶ {thinking_word}… (42s · ↓ 1234 tokens)\n"
        f"⏵⏵ bypass permissions on"
    )
    # prev_content == content → normally would be stalled; spinner must override
    state, _ = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=120.0,
        threshold=600.0,
    )
    assert state == "quiet", (
        f"Expected 'quiet' for thinking word {thinking_word!r}, got {state!r}"
    )


# ---------------------------------------------------------------------------
# 5. execute_recovery — context-level recycle policy
# ---------------------------------------------------------------------------


def _db_and_thread(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test-thread", session_id="")
    return db, thread_id


def test_execute_recovery_recycles_high_context_agent(tmp_path):
    """Incident 2026-06-15: alive_slow at 82% context must decommission + respawn.

    Previously: nudge was always sent → auto-compact → stall loop.
    After fix: context above threshold triggers recycle (spawn fresh agent).
    """
    from juggle_watchdog import execute_recovery

    db, thread_id = _db_and_thread(tmp_path)
    pane_content = (
        "claude.ai/code\n"  # CC UI marker → alive_slow
        "Sonnet 4.6(164.0k/200.0k) | 1% until auto-compact\n"
        "❯ "
    )
    mgr = MagicMock()
    mgr.verify_pane.return_value = True  # pane exists → alive_slow
    mgr.capture_pane.return_value = pane_content  # liveness recheck: same content
    mgr.spawn_agent.return_value = {
        "id": "new-agent-xxx",
        "pane_id": "%99",
        "status": "busy",
    }

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="implement feature X",
        watchdog_retried=0,
    )
    db.update_thread(thread_id, status="active")

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        pane_content,
        recovery_dir=tmp_path / "recovery",
        session_id="sid",
    )

    mgr.spawn_agent.assert_called_once()          # fresh agent spawned
    mgr.kill_pane.assert_called_once_with("%5")   # old bloated pane killed
    mgr._run_tmux.assert_not_called()             # no nudge sent


def test_execute_recovery_nudges_low_context_agent(tmp_path):
    """alive_slow agent below context threshold must be nudged, not recycled."""
    from juggle_watchdog import execute_recovery

    db, thread_id = _db_and_thread(tmp_path)
    pane_content = (
        "claude.ai/code\n"  # CC UI marker → alive_slow
        "Sonnet 4.6(40.0k/200.0k)\n"
        "❯ "
    )
    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.capture_pane.return_value = pane_content

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="implement feature X",
        watchdog_retried=0,
    )
    db.update_thread(thread_id, status="active")

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        pane_content,
        recovery_dir=tmp_path / "recovery",
        session_id="sid",
    )

    mgr.spawn_agent.assert_not_called()  # no recycle
    mgr._run_tmux.assert_called()        # nudge fired


def test_execute_recovery_no_recycle_when_context_unparseable(tmp_path):
    """Unparseable context % must fall back to nudge — no regression."""
    from juggle_watchdog import execute_recovery

    db, thread_id = _db_and_thread(tmp_path)
    pane_content = "claude.ai/code\n❯ "  # CC UI marker but no context %

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.capture_pane.return_value = pane_content

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=0,
    )
    db.update_thread(thread_id, status="active")

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        pane_content,
        recovery_dir=tmp_path / "recovery",
        session_id="sid",
    )

    mgr.spawn_agent.assert_not_called()  # no recycle


def test_execute_recovery_no_recycle_when_spinner_active_despite_high_context(tmp_path):
    """Agent actively thinking at high context must NOT be recycled.

    Active spinner signals the agent is making progress — interrupting would
    be worse than the context risk.
    """
    from juggle_watchdog import execute_recovery

    db, thread_id = _db_and_thread(tmp_path)
    pane_content = (
        "claude.ai/code\n"  # CC UI marker → alive_slow
        "✶ Garnishing… (42s · ↓ 1234 tokens)\n"
        "Sonnet 4.6(164.0k/200.0k)\n"
    )
    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.capture_pane.return_value = pane_content

    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="implement feature X",
        watchdog_retried=0,
    )
    db.update_thread(thread_id, status="active")

    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        pane_content,
        recovery_dir=tmp_path / "recovery",
        session_id="sid",
    )

    mgr.spawn_agent.assert_not_called()  # don't kill an actively working agent
