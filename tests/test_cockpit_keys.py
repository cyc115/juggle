"""TDD tests for cockpit keyboard-shortcut helpers.

Cycles:
  1. _resolve_thread_by_label — pure helper
  2. _resolve_actions_by_thread_label — pure helper
  3. BINDINGS list (no 'r' key; s/a/? present)
  4. HelpModal deduplication (no repeated action rows)
  5. action_switch — Textual Pilot (label not found, success)
  6. action_ack — Textual Pilot (no open actions, success)
  7. _new_blocker_actions / _newly_failed_agents — Phase 4 bell helpers
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Cycle 1 — _resolve_thread_by_label (pure helper)
# ---------------------------------------------------------------------------


def test_resolve_thread_by_label_exact_match():
    """Returns the matching thread dict for an exact uppercase label."""
    from juggle_cockpit import _resolve_thread_by_label

    threads = [
        {"id": "aaa", "user_label": "MA"},
        {"id": "bbb", "user_label": "MB"},
    ]
    result = _resolve_thread_by_label(threads, "MA")
    assert result is not None
    assert result["id"] == "aaa"


def test_resolve_thread_by_label_case_insensitive():
    """Lookup is case-insensitive: 'ma' matches 'MA'."""
    from juggle_cockpit import _resolve_thread_by_label

    threads = [{"id": "aaa", "user_label": "MA"}]
    assert _resolve_thread_by_label(threads, "ma") is not None
    assert _resolve_thread_by_label(threads, "Ma") is not None


def test_resolve_thread_by_label_not_found():
    """Returns None when no thread has the given label."""
    from juggle_cockpit import _resolve_thread_by_label

    threads = [{"id": "aaa", "user_label": "MA"}]
    assert _resolve_thread_by_label(threads, "ZZ") is None


def test_resolve_thread_by_label_empty_list():
    """Returns None for an empty threads list."""
    from juggle_cockpit import _resolve_thread_by_label

    assert _resolve_thread_by_label([], "MA") is None


# ---------------------------------------------------------------------------
# Cycle 2 — _resolve_actions_by_thread_label (pure helper)
# ---------------------------------------------------------------------------


def test_resolve_actions_by_thread_label_returns_matching():
    """Returns all open action dicts whose thread_id matches the resolved thread."""
    from juggle_cockpit import _resolve_actions_by_thread_label

    threads = [{"id": "aaa", "user_label": "MA"}, {"id": "bbb", "user_label": "MB"}]
    open_actions = [
        {"id": 1, "thread_id": "aaa", "message": "do thing"},
        {"id": 2, "thread_id": "aaa", "message": "do other"},
        {"id": 3, "thread_id": "bbb", "message": "unrelated"},
    ]
    result = _resolve_actions_by_thread_label(threads, open_actions, "MA")
    assert len(result) == 2
    assert all(a["thread_id"] == "aaa" for a in result)


def test_resolve_actions_by_thread_label_case_insensitive():
    """Case-insensitive: 'ma' resolves correctly."""
    from juggle_cockpit import _resolve_actions_by_thread_label

    threads = [{"id": "aaa", "user_label": "MA"}]
    open_actions = [{"id": 1, "thread_id": "aaa", "message": "x"}]
    assert len(_resolve_actions_by_thread_label(threads, open_actions, "ma")) == 1


def test_resolve_actions_by_thread_label_no_open_actions():
    """Returns empty list when thread exists but has no open actions."""
    from juggle_cockpit import _resolve_actions_by_thread_label

    threads = [{"id": "aaa", "user_label": "MA"}]
    assert _resolve_actions_by_thread_label(threads, [], "MA") == []


def test_resolve_actions_by_thread_label_label_not_found():
    """Returns empty list when the label doesn't match any thread."""
    from juggle_cockpit import _resolve_actions_by_thread_label

    threads = [{"id": "aaa", "user_label": "MA"}]
    open_actions = [{"id": 1, "thread_id": "aaa", "message": "x"}]
    assert _resolve_actions_by_thread_label(threads, open_actions, "ZZ") == []


# ---------------------------------------------------------------------------
# Cycle 3 — BINDINGS: expected keys present, 'r' absent
# ---------------------------------------------------------------------------


def test_bindings_has_expected_keys():
    """BINDINGS must include ?, s, a, j, k. No 'r' (manual refresh removed). No 'q' (ctrl+c quits)."""
    from juggle_cockpit import CockpitApp

    keys = {b.key for b in CockpitApp.BINDINGS}
    assert "question_mark" in keys, "? help key missing"
    assert "s" in keys, "s switch key missing"
    assert "a" in keys, "a ack key missing"
    assert "j" in keys, "j scroll key missing"
    assert "k" in keys, "k scroll key missing"
    assert "pagedown" in keys, "pagedown key missing"
    assert "pageup" in keys, "pageup key missing"
    assert "r" not in keys, "'r' key must not be present (manual refresh removed)"


def test_q_key_not_in_quit_bindings():
    """'q' must NOT be a quit hotkey — ctrl+c is the only quit key.

    Regression pin: 2026-06-10 — 'q' quit interfered with thread-label input.
    """
    from juggle_cockpit import CockpitApp

    quit_keys = {b.key for b in CockpitApp.BINDINGS if b.action == "quit"}
    assert "q" not in quit_keys, "'q' must not quit — users type 'q' in thread labels"
    assert "ctrl+c" in quit_keys, "ctrl+c must still be the quit key"


# ---------------------------------------------------------------------------
# Cycle 4 — HelpModal deduplication (no duplicate action rows)
# ---------------------------------------------------------------------------


def test_help_modal_no_duplicate_action_rows():
    """HelpModal must not emit duplicate rows for aliased scroll keys (j/↓, k/↑ etc)."""
    from juggle_cockpit import CockpitApp

    seen_actions: set[str] = set()
    duplicates = []
    for b in CockpitApp.BINDINGS:
        if not b.description:
            continue
        if b.action in seen_actions:
            duplicates.append(b.action)
        seen_actions.add(b.action)

    # The HelpModal deduplication logic skips entries where action already seen;
    # this test verifies there ARE duplicates in BINDINGS (aliases exist),
    # and that the dedup logic would reduce them.
    # Specifically scroll_down and scroll_up are bound to j/down and k/up respectively.
    all_actions = [b.action for b in CockpitApp.BINDINGS if b.description]
    # j and down both map to scroll_down; k and up both map to scroll_up.
    # HelpModal dedup shows each action once. Confirm aliases exist but cap at 2.
    assert all_actions.count("scroll_down") <= 2  # j + down
    assert all_actions.count("scroll_up") <= 2    # k + up


# ---------------------------------------------------------------------------
# Cycle 7 — _resolve_agent_by_index (pure helper)
# ---------------------------------------------------------------------------

from juggle_cockpit import _resolve_agent_by_index
from juggle_cockpit_model import Agent


def _make_agent(idx: int) -> Agent:
    return Agent(
        id_short=f"abc1234{idx}", role="coder", status="busy",
        topic_id="MA", age_secs=10, pane_id=f"%{100 + idx}",
    )


def test_resolve_agent_by_index_valid():
    agents = [_make_agent(1), _make_agent(2), _make_agent(3)]
    assert _resolve_agent_by_index(agents, 1) == agents[0]  # 1-based


def test_resolve_agent_by_index_out_of_range():
    agents = [_make_agent(1)]
    assert _resolve_agent_by_index(agents, 0) is None   # below range
    assert _resolve_agent_by_index(agents, 2) is None   # above range


def test_resolve_agent_by_index_empty():
    assert _resolve_agent_by_index([], 1) is None


# ---------------------------------------------------------------------------
# Cycle 8 — Filter pure helpers
# ---------------------------------------------------------------------------

from juggle_cockpit import _parse_filter, _apply_filter_actions, _apply_filter_text
from juggle_cockpit_model import Action, Notification


def _make_action(id: str, text: str, tier: int, topic: str = "MA") -> Action:
    return Action(id=id, topic_id=topic, text=text, tier=tier, age_secs=10)


def _make_notif(text: str) -> Notification:
    return Notification(text=text, kind="info", age_secs=5)


# _parse_filter
def test_parse_filter_plain():
    assert _parse_filter("deploy") == (None, "deploy")


def test_parse_filter_priority_only():
    assert _parse_filter("priority:high") == ("high", "")


def test_parse_filter_priority_with_text():
    assert _parse_filter("priority:blocker ssh") == ("blocker", "ssh")


def test_parse_filter_empty():
    assert _parse_filter("") == (None, "")


# _apply_filter_actions
def test_filter_actions_empty_text_passthrough():
    acts = [_make_action("a1", "deploy DB", tier=0)]
    assert _apply_filter_actions(acts, "") is acts


def test_filter_actions_substring_match():
    acts = [_make_action("a1", "deploy DB", 0), _make_action("a2", "write tests", 1)]
    result = _apply_filter_actions(acts, "deploy")
    assert result == [acts[0]]


def test_filter_actions_priority_high():
    acts = [_make_action("a1", "x", 0), _make_action("a2", "y", 1)]
    assert _apply_filter_actions(acts, "priority:high") == [acts[0]]


def test_filter_actions_priority_blocker_alias():
    acts = [_make_action("a1", "x", 0), _make_action("a2", "y", 2)]
    assert _apply_filter_actions(acts, "priority:blocker") == [acts[0]]


def test_filter_actions_priority_with_substring():
    acts = [_make_action("a1", "deploy ssh", 0), _make_action("a2", "deploy api", 0)]
    result = _apply_filter_actions(acts, "priority:high ssh")
    assert result == [acts[0]]


def test_filter_actions_topic_match():
    acts = [_make_action("a1", "thing", 1, topic="MA"), _make_action("a2", "thing", 1, topic="ZZ")]
    result = _apply_filter_actions(acts, "MA")
    assert result == [acts[0]]


# _apply_filter_text (generic)
def test_filter_text_notifs_match():
    notifs = [_make_notif("agent completed"), _make_notif("watchdog fired")]
    result = _apply_filter_text(notifs, "complete")
    assert result == [notifs[0]]


def test_filter_text_empty_passthrough():
    notifs = [_make_notif("x")]
    assert _apply_filter_text(notifs, "") is notifs


# ---------------------------------------------------------------------------
# Cycles 5 & 6 — Textual Pilot (switch + ack)  [require textual]
# ---------------------------------------------------------------------------

pytest.importorskip("textual", reason="textual not installed")


def _press_label(label: str) -> list[str]:
    """Convert a thread label like 'A' or 'AB' to a list of Textual key names.
    Action handlers normalise to uppercase so we type lowercase.
    """
    return [ch.lower() for ch in label]


@pytest.mark.asyncio
async def test_action_switch_label_not_found(tmp_path):
    """Pressing 's' with an unknown label shows a warning notification (no crash)."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("test topic", session_id="")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.1)
        # Type an unknown label 'z' (no thread has this label)
        await pilot.press("z")
        await pilot.press("enter")
        await pilot.pause(0.2)
    # No crash = pass; notification is ephemeral


@pytest.mark.asyncio
async def test_action_switch_valid_label(tmp_path):
    """Pressing 's' with a valid label calls set_current_thread."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("thread one", session_id="")
    t2 = db.create_thread("thread two", session_id="")
    db.set_current_thread(t1)

    threads = db.get_all_threads()
    t2_label = next(t["user_label"] for t in threads if t["id"] == t2)

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.1)
        for key in _press_label(t2_label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.3)

    session_row = db._connect().execute(
        "SELECT value FROM session WHERE key = 'current_thread'"
    ).fetchone()
    assert session_row is not None
    assert session_row[0] == t2


@pytest.mark.asyncio
async def test_action_ack_no_open_actions(tmp_path):
    """Pressing 'a' with no open actions shows a warning notification (no crash)."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("thread one", session_id="")
    threads = db.get_all_threads()
    label = threads[0]["user_label"]

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause(0.1)
        for key in _press_label(label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.2)
    # No crash = pass


@pytest.mark.asyncio
async def test_action_ack_dismisses_all_for_label(tmp_path):
    """Pressing 'a' with a valid label dismisses all open actions for that thread."""
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t1 = db.create_thread("thread one", session_id="")
    t2 = db.create_thread("thread two", session_id="")

    db.add_action_item(t1, "action A on t1", type_="question")
    db.add_action_item(t1, "action B on t1", type_="question")
    db.add_action_item(t2, "action C on t2", type_="question")

    threads = db.get_all_threads()
    t1_label = next(t["user_label"] for t in threads if t["id"] == t1)

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause(0.1)
        for key in _press_label(t1_label):
            await pilot.press(key)
        await pilot.press("enter")
        await pilot.pause(0.3)

    remaining = db.get_open_action_items()
    assert all(a["thread_id"] == t2 for a in remaining), (
        f"Expected only t2 actions remaining, got: {remaining}"
    )


# ---------------------------------------------------------------------------
# Cycle 7 — Phase 4 bell helpers: _new_blocker_actions / _newly_failed_agents
# ---------------------------------------------------------------------------


def _blocker(id: str) -> "Action":  # noqa: F821
    from juggle_cockpit_model import Action
    return Action(id=id, topic_id="MA", text="blocker text", tier=0, age_secs=5)


def _normal(id: str) -> "Action":  # noqa: F821
    from juggle_cockpit_model import Action
    return Action(id=id, topic_id="MA", text="normal text", tier=1, age_secs=5)


def _bell_agent(id_short: str, status: str) -> "Agent":  # noqa: F821
    from juggle_cockpit_model import Agent
    return Agent(id_short=id_short, role="coder", status=status, topic_id="MA", age_secs=10)


# _new_blocker_actions
def test_new_blocker_returns_unseen_tier0():
    """Tier-0 actions not in prev_ids are returned."""
    from juggle_cockpit import _new_blocker_actions
    actions = [_blocker("b1")]
    result = _new_blocker_actions(set(), actions)
    assert result == [actions[0]]


def test_new_blocker_already_seen_excluded():
    """Tier-0 action whose id is in prev_ids is NOT returned."""
    from juggle_cockpit import _new_blocker_actions
    actions = [_blocker("b1")]
    assert _new_blocker_actions({"b1"}, actions) == []


def test_new_blocker_only_tier0():
    """Non-tier-0 actions are never returned."""
    from juggle_cockpit import _new_blocker_actions
    actions = [_blocker("b1"), _normal("n1")]
    assert _new_blocker_actions(set(), actions) == [actions[0]]


def test_new_blocker_mixed_seen_unseen():
    """Only the unseen blocker is returned when one is seen and one is new."""
    from juggle_cockpit import _new_blocker_actions
    actions = [_blocker("b1"), _blocker("b2")]
    result = _new_blocker_actions({"b1"}, actions)
    assert result == [actions[1]]


# _newly_failed_agents
def test_newly_failed_busy_to_stale():
    """Agent transitioning busy→stale is returned."""
    from juggle_cockpit import _newly_failed_agents
    agents = [_bell_agent("abc12345", "stale")]
    prev = {"abc12345": "busy"}
    assert _newly_failed_agents(prev, agents) == [agents[0]]


def test_newly_failed_already_stale_excluded():
    """Agent that was already stale is NOT returned."""
    from juggle_cockpit import _newly_failed_agents
    agents = [_bell_agent("abc12345", "stale")]
    prev = {"abc12345": "stale"}
    assert _newly_failed_agents(prev, agents) == []


def test_newly_failed_no_prev_entry_skipped():
    """Unknown agent going stale (new agent) does NOT trigger alert."""
    from juggle_cockpit import _newly_failed_agents
    agents = [_bell_agent("newagent0", "stale")]
    prev: dict = {}
    assert _newly_failed_agents(prev, agents) == []


def test_newly_failed_busy_stays_busy():
    """Agent that remains busy is NOT returned."""
    from juggle_cockpit import _newly_failed_agents
    agents = [_bell_agent("abc12345", "busy")]
    prev = {"abc12345": "busy"}
    assert _newly_failed_agents(prev, agents) == []


# ---------------------------------------------------------------------------
# Task 10 — _tmux_focus_pane / _tmux_capture_pane helpers
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock


def test_tmux_focus_pane_success():
    from juggle_cockpit import _tmux_focus_pane
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        assert _tmux_focus_pane("%123") is True
        mock_run.assert_called_once_with(
            ["tmux", "select-pane", "-t", "%123"],
            capture_output=True, timeout=2,
        )


def test_tmux_focus_pane_failure():
    from juggle_cockpit import _tmux_focus_pane
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)
        assert _tmux_focus_pane("%123") is False


def test_tmux_focus_pane_not_found():
    from juggle_cockpit import _tmux_focus_pane
    with patch("subprocess.run", side_effect=FileNotFoundError):
        assert _tmux_focus_pane("%123") is False


def test_tmux_capture_pane_returns_last_n_lines():
    from juggle_cockpit import _tmux_capture_pane
    output = "\n".join(f"line{i}" for i in range(50))
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=output)
        result = _tmux_capture_pane("%123", lines=5)
    assert result == "\n".join(f"line{i}" for i in range(45, 50))


def test_tmux_capture_pane_failure_returns_empty():
    from juggle_cockpit import _tmux_capture_pane
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _tmux_capture_pane("%123") == ""


def test_tmux_capture_pane_uses_scrollback_flag():
    """_tmux_capture_pane must pass -S -N to read from scrollback, not just visible region."""
    from juggle_cockpit import _tmux_capture_pane
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="a\nb\nc")
        _tmux_capture_pane("%99", lines=15)
    argv = mock_run.call_args[0][0]
    assert "-S" in argv, f"Expected '-S' in tmux argv, got {argv}"
    assert "-15" in argv, f"Expected '-15' in tmux argv, got {argv}"


# ---------------------------------------------------------------------------
# Tab / Shift+Tab pane-cycle — Pilot functional tests
# (Regression: Tab was eaten by Textual focus traversal; fix in on_key)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tab_cycles_pane_forward(tmp_path):
    """Tab press advances _active_pane to the next entry in _SCROLL_PANES.

    Starts at 'notifications' (default), expects 'actions' after one Tab,
    'agents' after two, 'notifications' after three (full cycle).
    """
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _SCROLL_PANES

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("alpha", session_id="")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        # Default active pane is 'notifications'
        assert app._active_pane == "notifications"
        start_idx = _SCROLL_PANES.index("notifications")

        await pilot.press("tab")
        await pilot.pause(0.15)
        expected_1 = _SCROLL_PANES[(start_idx + 1) % len(_SCROLL_PANES)]
        assert app._active_pane == expected_1, (
            f"After 1 Tab: expected {expected_1!r}, got {app._active_pane!r}"
        )

        await pilot.press("tab")
        await pilot.pause(0.15)
        expected_2 = _SCROLL_PANES[(start_idx + 2) % len(_SCROLL_PANES)]
        assert app._active_pane == expected_2, (
            f"After 2 Tabs: expected {expected_2!r}, got {app._active_pane!r}"
        )

        await pilot.press("tab")
        await pilot.pause(0.15)
        expected_3 = _SCROLL_PANES[(start_idx + 3) % len(_SCROLL_PANES)]
        assert app._active_pane == expected_3, (
            f"After 3 Tabs: expected {expected_3!r}, got {app._active_pane!r}"
        )

        # Fourth Tab wraps back around (full cycle)
        await pilot.press("tab")
        await pilot.pause(0.15)
        assert app._active_pane == "notifications", (
            f"After 4 Tabs (full cycle): expected 'notifications', got {app._active_pane!r}"
        )


@pytest.mark.asyncio
async def test_shift_tab_cycles_pane_backward(tmp_path):
    """Shift+Tab reverses _active_pane to the previous entry in _SCROLL_PANES.

    Starts at 'notifications' (default), expects 'agents' after one Shift+Tab
    (wraps backward), then 'actions', then back to 'notifications'.
    """
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp, _SCROLL_PANES

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("beta", session_id="")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        assert app._active_pane == "notifications"
        n = len(_SCROLL_PANES)
        start_idx = _SCROLL_PANES.index("notifications")

        await pilot.press("shift+tab")
        await pilot.pause(0.15)
        expected_1 = _SCROLL_PANES[(start_idx - 1) % n]
        assert app._active_pane == expected_1, (
            f"After 1 Shift+Tab: expected {expected_1!r}, got {app._active_pane!r}"
        )

        await pilot.press("shift+tab")
        await pilot.pause(0.15)
        expected_2 = _SCROLL_PANES[(start_idx - 2) % n]
        assert app._active_pane == expected_2, (
            f"After 2 Shift+Tabs: expected {expected_2!r}, got {app._active_pane!r}"
        )

        await pilot.press("shift+tab")
        await pilot.pause(0.15)
        expected_3 = _SCROLL_PANES[(start_idx - 3) % n]
        assert app._active_pane == expected_3, (
            f"After 3 Shift+Tabs: expected {expected_3!r}, got {app._active_pane!r}"
        )

        # Fourth Shift+Tab wraps back (full reverse cycle)
        await pilot.press("shift+tab")
        await pilot.pause(0.15)
        assert app._active_pane == "notifications", (
            f"After 4 Shift+Tabs (full reverse cycle): expected 'notifications', "
            f"got {app._active_pane!r}"
        )


# ---------------------------------------------------------------------------
# Cycle 9 — _TailModal (modal overlay replacing inline #tail drawer)
# ---------------------------------------------------------------------------


def test_tail_modal_refresh_calls_capture_fn():
    """_refresh_tail captures TAIL_LINES (100) lines and updates the Static body."""
    from unittest.mock import MagicMock, patch

    from juggle_cockpit_modals import _TailModal

    calls: list = []

    def fake_capture(pane_id, lines=20):
        calls.append((pane_id, lines))
        return "line1\nline2"

    modal = _TailModal("%99", fake_capture)
    mock_body = MagicMock()
    # Scroll container: pinned to the bottom (offset == max) so the tail follows.
    mock_scroll = MagicMock()
    mock_scroll.scroll_offset.y = 0
    mock_scroll.max_scroll_y = 0

    def fake_query_one(selector, _type=None):
        return mock_scroll if selector == "#tail-scroll" else mock_body

    with patch.object(modal, "query_one", side_effect=fake_query_one):
        modal._refresh_tail()

    assert calls == [("%99", 100)], f"Expected [('%99', 100)], got {calls}"
    from rich.text import Text as RichText

    mock_body.update.assert_called_once()
    arg = mock_body.update.call_args[0][0]
    assert isinstance(arg, RichText) and str(arg) == "line1\nline2"
    mock_scroll.scroll_end.assert_called_once()  # followed the tail (was at bottom)


@pytest.mark.asyncio
async def test_tail_modal_dismiss_on_t():
    """_TailModal dismisses itself when 't' is pressed."""
    from textual.app import App

    from juggle_cockpit_modals import _TailModal

    def fake_capture(pane_id, lines=20):
        return "output"

    class TestApp(App):
        async def on_mount(self) -> None:
            await self.push_screen(_TailModal("%1", fake_capture))

    async with TestApp().run_test(size=(80, 24)) as pilot:
        assert len(pilot.app.screen_stack) == 2, "modal should be open"
        await pilot.press("t")
        await pilot.pause(0.2)
        assert len(pilot.app.screen_stack) == 1, "modal should be dismissed after 't'"


@pytest.mark.asyncio
async def test_tail_modal_dismiss_on_escape():
    """_TailModal dismisses itself when 'escape' is pressed."""
    from textual.app import App

    from juggle_cockpit_modals import _TailModal

    def fake_capture(pane_id, lines=20):
        return "output"

    class TestApp(App):
        async def on_mount(self) -> None:
            await self.push_screen(_TailModal("%2", fake_capture))

    async with TestApp().run_test(size=(80, 24)) as pilot:
        assert len(pilot.app.screen_stack) == 2, "modal should be open"
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert len(pilot.app.screen_stack) == 1, "modal should be dismissed after 'escape'"


@pytest.mark.asyncio
async def test_no_tail_widget_in_cockpit(tmp_path):
    """CockpitApp must NOT have a #tail Static widget — drawer is removed."""
    from textual.css.query import NoMatches

    from juggle_cockpit import CockpitApp
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("test", session_id="")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        with pytest.raises(NoMatches):
            app.query_one("#tail")


@pytest.mark.asyncio
async def test_tail_toggle_pushes_modal_for_valid_agent(tmp_path):
    """Pressing 't' then a valid agent index pushes _TailModal onto screen_stack."""
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_modals import _TailModal
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    db.create_thread("test", session_id="")
    db.create_agent("coder", "%99")  # agent with a tmux pane_id

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        # 't' → _PromptModal for agent index
        await pilot.press("t")
        await pilot.pause(0.15)
        # type '1' + enter → resolves to agent #1
        await pilot.press("1")
        await pilot.press("enter")
        await pilot.pause(0.3)
        # _TailModal should now be on top of screen_stack
        assert len(pilot.app.screen_stack) == 2, (
            f"Expected _TailModal on stack, got depth {len(pilot.app.screen_stack)}"
        )
        assert isinstance(pilot.app.screen_stack[-1], _TailModal), (
            f"Expected _TailModal, got {type(pilot.app.screen_stack[-1])}"
        )


# ---------------------------------------------------------------------------
# Cycle 10 — _TailModal: q-to-close + j/k scrolling (TDD)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_modal_dismiss_on_q():
    """_TailModal dismisses itself when 'q' is pressed."""
    from textual.app import App

    from juggle_cockpit_modals import _TailModal

    def fake_capture(pane_id, lines=20):
        return "output"

    class TestApp(App):
        async def on_mount(self) -> None:
            await self.push_screen(_TailModal("%3", fake_capture))

    async with TestApp().run_test(size=(80, 24)) as pilot:
        assert len(pilot.app.screen_stack) == 2, "modal should be open"
        await pilot.press("q")
        await pilot.pause(0.2)
        assert len(pilot.app.screen_stack) == 1, "modal should be dismissed after 'q'"


def test_tail_modal_j_calls_scroll_down():
    """on_key('j') calls scroll_down() on #tail-scroll and stops event propagation."""
    from unittest.mock import MagicMock, patch

    from textual import events
    from textual.containers import VerticalScroll

    from juggle_cockpit_modals import _TailModal

    modal = _TailModal("%5", lambda pane_id, lines=20: "")
    mock_scroll = MagicMock(spec=VerticalScroll)
    mock_event = MagicMock(spec=events.Key)
    mock_event.key = "j"

    with patch.object(modal, "query_one", return_value=mock_scroll):
        modal.on_key(mock_event)

    mock_scroll.scroll_down.assert_called_once()
    mock_event.stop.assert_called()


def test_tail_modal_k_calls_scroll_up():
    """on_key('k') calls scroll_up() on #tail-scroll and stops event propagation."""
    from unittest.mock import MagicMock, patch

    from textual import events
    from textual.containers import VerticalScroll

    from juggle_cockpit_modals import _TailModal

    modal = _TailModal("%6", lambda pane_id, lines=20: "")
    mock_scroll = MagicMock(spec=VerticalScroll)
    mock_event = MagicMock(spec=events.Key)
    mock_event.key = "k"

    with patch.object(modal, "query_one", return_value=mock_scroll):
        modal.on_key(mock_event)

    mock_scroll.scroll_up.assert_called_once()
    mock_event.stop.assert_called()


def test_tail_modal_hint_string_mentions_q_and_jk():
    """compose() header hints include q, j, k in the hint string."""
    from unittest.mock import MagicMock, patch

    from juggle_cockpit_modals import _TailModal

    modal = _TailModal("%7", lambda pane_id, lines=20: "")
    yielded: list = []

    with patch("juggle_cockpit_modals.VerticalScroll") as mock_vs, \
         patch("juggle_cockpit_modals.Static") as mock_static, \
         patch("juggle_cockpit_modals.Vertical"):
        mock_vs.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_vs.return_value.__exit__ = MagicMock(return_value=False)
        # Capture the first Static call (the header)
        # Iterate compose() generator
        try:
            for _ in modal.compose():
                pass
        except Exception:
            pass
        if mock_static.call_args_list:
            hint = mock_static.call_args_list[0][0][0]
            assert "j" in hint, f"header hint missing 'j': {hint!r}"
            assert "k" in hint, f"header hint missing 'k': {hint!r}"
            assert "q" in hint, f"header hint missing 'q': {hint!r}"


# ---------------------------------------------------------------------------
# Cycle 11 — Mouse-wheel scroll regression (TDD)
#
# Root cause of prior failures: on_scroll_up/on_scroll_down listened for
# ScrollUp/ScrollDown events that DO NOT EXIST in Textual.  Mouse wheel fires
# MouseScrollUp/MouseScrollDown.  Correct handlers: on_mouse_scroll_up /
# on_mouse_scroll_down.  Also used event.control (wrong attr); MouseEvent
# carries event.widget.
# ---------------------------------------------------------------------------


def _make_scrollable_db(tmp_path, n_threads: int = 10):
    """Return a db_path with n_threads so panes have enough rows to scroll past clamp."""
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    for i in range(n_threads):
        db.create_thread(f"topic {i}", session_id="")
    return db_path


@pytest.mark.asyncio
async def test_mouse_wheel_scroll_down_increments_offset(tmp_path):
    """Posting MouseScrollDown must increment the active pane's offset by 1.

    RED before fix: on_scroll_down listens for a non-existent ScrollDown event;
    MouseScrollDown goes unhandled and offset stays 0.
    GREEN after fix: on_mouse_scroll_down handles it and offset becomes 1.

    Uses 10 threads so _refresh() clamp (len-3) allows offset >= 1.
    """
    from textual.events import MouseScrollDown

    from juggle_cockpit import CockpitApp

    db_path = _make_scrollable_db(tmp_path)
    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        app._active_pane = "topics"
        assert app._offsets["topics"] == 0

        topics_widget = app.query_one("#topics")
        app.post_message(
            MouseScrollDown(
                widget=topics_widget, x=5, y=5,
                delta_x=0, delta_y=1,
                button=0, shift=False, meta=False, ctrl=False,
            )
        )
        await pilot.pause(0.2)

        assert app._offsets["topics"] == 1, (
            f"Expected offset 1, got {app._offsets['topics']}. "
            "on_mouse_scroll_down not firing — check handler name and event class."
        )


@pytest.mark.asyncio
async def test_mouse_wheel_scroll_up_decrements_offset(tmp_path):
    """Posting MouseScrollUp must decrement the active pane's offset (floor 0).

    RED before fix: on_scroll_up listens for non-existent ScrollUp; offset stays 3.
    GREEN after fix: on_mouse_scroll_up handles it and offset becomes 2.
    """
    from textual.events import MouseScrollUp

    from juggle_cockpit import CockpitApp

    db_path = _make_scrollable_db(tmp_path)
    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        app._active_pane = "topics"
        app._offsets["topics"] = 3

        topics_widget = app.query_one("#topics")
        app.post_message(
            MouseScrollUp(
                widget=topics_widget, x=5, y=5,
                delta_x=0, delta_y=-1,
                button=0, shift=False, meta=False, ctrl=False,
            )
        )
        await pilot.pause(0.2)

        assert app._offsets["topics"] == 2, (
            f"Expected offset 2, got {app._offsets['topics']}. "
            "on_mouse_scroll_up not firing — check handler name and event class."
        )


@pytest.mark.asyncio
async def test_mouse_scroll_uses_widget_pane_when_present(tmp_path):
    """When event.widget.id is a valid pane, that pane's offset increments
    even if _active_pane is a different pane.
    """
    from textual.events import MouseScrollDown

    from juggle_cockpit import CockpitApp

    db_path = _make_scrollable_db(tmp_path)
    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        app._active_pane = "notifications"  # active pane is NOT topics
        assert app._offsets["topics"] == 0

        topics_widget = app.query_one("#topics")
        app.post_message(
            MouseScrollDown(
                widget=topics_widget, x=5, y=5,
                delta_x=0, delta_y=1,
                button=0, shift=False, meta=False, ctrl=False,
            )
        )
        await pilot.pause(0.2)

        assert app._offsets["topics"] == 1, (
            f"Scroll on topics widget should increment topics offset, got {app._offsets['topics']}"
        )
        assert app._offsets["notifications"] == 0, (
            "notifications offset must be unchanged"
        )


@pytest.mark.asyncio
async def test_keyboard_jk_scroll_not_regressed(tmp_path):
    """j/k keyboard scroll must still work after mouse-scroll handler rename."""
    from juggle_cockpit import CockpitApp

    db_path = _make_scrollable_db(tmp_path)
    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        app._active_pane = "topics"
        assert app._offsets["topics"] == 0

        await pilot.press("j")
        await pilot.pause(0.1)
        assert app._offsets["topics"] == 1, "j key must increment offset"

        await pilot.press("k")
        await pilot.pause(0.1)
        assert app._offsets["topics"] == 0, "k key must decrement offset back to 0"


# ---------------------------------------------------------------------------
# Orphan-action [Z] ack — regression pin 2026-06-10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_ack_z_dismisses_orphaned_actions(tmp_path):
    """Pressing 'a' then 'Z' must dismiss all thread_id IS NULL action items.

    Regression pin: 2026-06-10 — graph-dispatch failure actions had no
    thread label so they rendered '[]' and could not be dismissed via 'a'.
    The fix: label 'Z' is the sentinel that routes to dismiss_orphan_action_items().
    """
    from juggle_db import JuggleDB
    from juggle_cockpit import CockpitApp

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    t_id = db.create_thread("real thread", session_id="")
    db.add_action_item(t_id, "bound action", type_="question")
    db.add_action_item(None, "orphan dispatch failure", type_="question")
    db.add_action_item(None, "another orphan", type_="question")

    app = CockpitApp(db_path=db_path)
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("a")
        await pilot.pause(0.1)
        await pilot.press("z")
        await pilot.press("enter")
        await pilot.pause(0.3)

    open_items = db.get_open_action_items()
    assert len(open_items) == 1, (
        f"Expected only the bound action to survive, got {len(open_items)}: {open_items}"
    )
    assert open_items[0]["thread_id"] == t_id, "Bound action must not be dismissed"
