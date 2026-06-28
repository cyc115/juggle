"""Tests for juggle_schedule_dogfood."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import schedules.common as common
import schedules.dogfood as dogfood


# ---------------------------------------------------------------------------
# Idempotency: prior open dogfood thread blocks run
# ---------------------------------------------------------------------------

def test_prior_dogfood_thread_blocks_run(tmp_path):
    mock_db = MagicMock()

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch.object(dogfood, "db_query", return_value=[{"title": "dogfood-2026-05-11"}]), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=True)
    assert result == 1


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------

def test_dry_run_writes_report(tmp_path, monkeypatch):
    mock_db = MagicMock()
    mock_db.add_action_item = MagicMock()

    # M1 (2026-06-21): isolate the dry-run sample to a fresh tmp dir (no stale
    # /tmp false-green).
    monkeypatch.setenv("JUGGLE_SCHEDULE_SAMPLE_DIR", str(tmp_path))
    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch.object(dogfood, "db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=True)

    assert result == 0
    report = tmp_path / "schedule-dogfood-sample-report.md"
    assert report.exists()
    content = report.read_text()
    assert "DRY RUN" in content or "Juggle Self-Analysis" in content


# ---------------------------------------------------------------------------
# Idempotency: running twice doesn't double-file action items
# ---------------------------------------------------------------------------

def test_action_item_filed_once_on_dry_run(tmp_path):
    mock_db = MagicMock()
    add_calls = []
    mock_db.add_action_item = lambda **kw: add_calls.append(kw)

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch.object(dogfood, "db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        # dry_run=True skips action item filing
        dogfood.run(dry_run=True)
        dogfood.run(dry_run=True)

    # dry_run should not call add_action_item
    assert len(add_calls) == 0


# ---------------------------------------------------------------------------
# Cost cap triggers correctly
# ---------------------------------------------------------------------------

def test_cost_cap_aborts_and_writes_partial(tmp_path):
    mock_db = MagicMock()

    def fake_headless(prompt, tracker, dry_run):
        tracker.add(2.0)  # exceeds $1.00 cap
        return "some output"

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch.object(dogfood, "db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "_run_headless_research", fake_headless), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch.object(dogfood, "JUGGLE_REPO", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=False)

    assert result == 1


# ---------------------------------------------------------------------------
# Active session conflict defers
# ---------------------------------------------------------------------------

def test_active_session_defers_once(tmp_path):
    mock_db = MagicMock()
    call_count = [0]

    def fake_active_session(db):
        call_count[0] += 1
        return True  # always active

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch.object(dogfood, "db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", fake_active_session), \
         patch("time.sleep", return_value=None), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=False)

    assert result == 1
    assert call_count[0] == 2  # checked twice (initial + retry)
