"""Tests for juggle_schedule_common shared infrastructure."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import juggle_schedule_common as common


# ---------------------------------------------------------------------------
# State / idempotency
# ---------------------------------------------------------------------------

def test_load_state_empty(tmp_path):
    with patch.object(common, "STATE_FILE", tmp_path / "nonexistent.json"):
        assert common.load_state() == {}


def test_save_and_load_state(tmp_path):
    state_file = tmp_path / "state.json"
    with patch.object(common, "STATE_FILE", state_file), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        common.save_state({"dogfood": {"last_success": "2026-05-18T03:00:00+00:00"}})
        loaded = common.load_state()
        assert "dogfood" in loaded


def test_mark_run_complete_writes_state(tmp_path):
    state_file = tmp_path / "state.json"
    with patch.object(common, "STATE_FILE", state_file), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        common.mark_run_complete("dogfood")
        state = json.loads(state_file.read_text())
        assert "dogfood" in state
        assert "last_success" in state["dogfood"]


def test_last_run_ts_none_when_no_state(tmp_path):
    with patch.object(common, "STATE_FILE", tmp_path / "nonexistent.json"):
        assert common.last_run_ts("dogfood") is None


def test_last_run_ts_parses_correctly(tmp_path):
    state_file = tmp_path / "state.json"
    ts = "2026-05-18T03:00:00+00:00"
    state_file.write_text(json.dumps({"reflect": {"last_success": ts}}))
    with patch.object(common, "STATE_FILE", state_file):
        result = common.last_run_ts("reflect")
        assert result is not None
        assert result.year == 2026


def test_load_state_invalid_json(tmp_path):
    state_file = tmp_path / "invalid.json"
    state_file.write_text("not valid json {[")
    with patch.object(common, "STATE_FILE", state_file):
        assert common.load_state() == {}


def test_last_run_ts_invalid_iso_string(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"reflect": {"last_success": "not-a-date"}}))
    with patch.object(common, "STATE_FILE", state_file):
        assert common.last_run_ts("reflect") is None


def test_mark_run_complete_overwrites_existing(tmp_path):
    state_file = tmp_path / "state.json"
    old_ts = "2026-05-10T00:00:00+00:00"
    state_file.write_text(json.dumps({"dogfood": {"last_success": old_ts}}))
    with patch.object(common, "STATE_FILE", state_file), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        common.mark_run_complete("dogfood")
        state = json.loads(state_file.read_text())
        assert state["dogfood"]["last_success"] != old_ts


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------

def test_cost_tracker_accumulates():
    ct = common.CostTracker(cap_usd=1.0, routine="test")
    ct.add(0.25)
    ct.add(0.50)
    assert abs(ct.total - 0.75) < 0.0001


def test_cost_tracker_raises_on_exceed():
    ct = common.CostTracker(cap_usd=0.10, routine="test")
    with pytest.raises(common.CostCapExceeded):
        ct.add(0.50)


def test_cost_tracker_estimate_sonnet():
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    cost = ct.estimate_from_tokens(1_000_000, 100_000, model="claude-sonnet-4-6")
    assert 4.0 < cost < 5.0


def test_cost_tracker_estimate_haiku():
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    cost = ct.estimate_from_tokens(1_000_000, 100_000, model="claude-haiku-4-5")
    assert 1.0 < cost < 1.5


def test_cost_tracker_haiku_cheaper_than_sonnet():
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    haiku = ct.estimate_from_tokens(1_000_000, 100_000, model="claude-haiku-4-5")
    sonnet = ct.estimate_from_tokens(1_000_000, 100_000, model="claude-sonnet-4-6")
    assert haiku < sonnet


# ---------------------------------------------------------------------------
# gh_issue_exists
# ---------------------------------------------------------------------------

def test_gh_issue_exists_false_on_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("network")):
        assert common.gh_issue_exists("some title") is False


def test_gh_issue_exists_false_when_no_match():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([{"title": "other title", "createdAt": "2026-05-18T00:00:00Z"}])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("some title") is False


def test_gh_issue_exists_true_when_match():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([
        {"title": "exact match", "createdAt": datetime.now(timezone.utc).isoformat()}
    ])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("exact match", days=30) is True


def test_gh_issue_exists_respects_days_window():
    old_ts = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 60 * 86400, tz=timezone.utc
    ).isoformat()
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([{"title": "old issue", "createdAt": old_ts}])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("old issue", days=30) is False
        assert common.gh_issue_exists("old issue", days=90) is True


def test_gh_issue_exists_invalid_date():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([{"title": "exact match", "createdAt": "not-a-date"}])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("exact match") is False


# ---------------------------------------------------------------------------
# gh_create_issue
# ---------------------------------------------------------------------------

def test_gh_create_issue_dry_run_returns_none():
    assert common.gh_create_issue("title", "body", dry_run=True) is None


def test_gh_create_issue_success():
    mock_result = MagicMock()
    mock_result.stdout = "https://github.com/owner/repo/issues/42"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_create_issue("test issue", "test body")
        assert result == "https://github.com/owner/repo/issues/42"


def test_gh_create_issue_label_creation_failure():
    """gh_create_issue should handle _ensure_gh_label failures gracefully."""
    mock_result = MagicMock()
    mock_result.stdout = "https://github.com/owner/repo/issues/42"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result), \
         patch("juggle_schedule_common._ensure_gh_label", side_effect=Exception("label error")):
        result = common.gh_create_issue("issue", "body", labels=["bug"])
        assert result is not None


def test_gh_create_issue_handles_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("gh error")):
        assert common.gh_create_issue("title", "body") is None


def test_gh_create_issue_strips_output():
    mock_result = MagicMock()
    mock_result.stdout = "  https://github.com/owner/repo/issues/42  \n"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_create_issue("title", "body")
        assert result == "https://github.com/owner/repo/issues/42"


# ---------------------------------------------------------------------------
# gh_pr_list_head
# ---------------------------------------------------------------------------

def test_gh_pr_list_head_success():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([{"number": 42, "title": "fix", "state": "OPEN"}])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_pr_list_head("cyc_fix")
        assert len(result) == 1
        assert result[0]["number"] == 42


def test_gh_pr_list_head_empty():
    mock_result = MagicMock()
    mock_result.stdout = "[]"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_pr_list_head("nonexistent") == []


def test_gh_pr_list_head_handles_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("gh error")):
        assert common.gh_pr_list_head("cyc_fix") == []


def test_gh_pr_list_head_empty_stdout():
    mock_result = MagicMock()
    mock_result.stdout = None
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_pr_list_head("cyc_fix") == []


# ---------------------------------------------------------------------------
# claude_p
# ---------------------------------------------------------------------------

def test_claude_p_success():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "hello world", "usage": {"input_tokens": 10, "output_tokens": 5}})
    with patch("subprocess.run", return_value=mock_result):
        assert common.claude_p("test prompt") == "hello world"


def test_claude_p_with_cost_tracker():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "out", "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}})
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    with patch("subprocess.run", return_value=mock_result):
        common.claude_p("test", cost_tracker=ct)
        assert ct.total > 0


def test_claude_p_fallback_to_text():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "plain text"
    with patch("subprocess.run", return_value=mock_result):
        assert common.claude_p("test") == "plain text"


def test_claude_p_error_returns_empty():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "error"
    with patch("subprocess.run", return_value=mock_result):
        assert common.claude_p("test") == ""


def test_claude_p_content_field_fallback():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"content": "from content field"})
    with patch("subprocess.run", return_value=mock_result):
        assert common.claude_p("test") == "from content field"


# ---------------------------------------------------------------------------
# today_str / days_ago_iso
# ---------------------------------------------------------------------------

def test_today_str_format():
    s = common.today_str()
    parts = s.split("-")
    assert len(parts) == 3 and len(parts[0]) == 4


def test_days_ago_iso_is_past():
    ago = common.days_ago_iso(7)
    dt = datetime.fromisoformat(ago)
    diff = (datetime.now(timezone.utc) - dt).total_seconds()
    assert 6.5 * 86400 < diff < 7.5 * 86400


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

def test_write_report_dry_run(tmp_path):
    out = tmp_path / "report.md"
    tmp = tmp_path / "tmp_report.md"
    result = common.write_report(out, "hello", dry_run=True, tmp_override=tmp)
    assert result == tmp
    assert tmp.read_text() == "hello"
    assert not out.exists()


def test_write_report_live(tmp_path):
    out = tmp_path / "subdir" / "report.md"
    result = common.write_report(out, "world")
    assert result == out and out.read_text() == "world"


# ---------------------------------------------------------------------------
# gh_run
# ---------------------------------------------------------------------------

def test_gh_run_calls_gh_prefix():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        common.gh_run(["issue", "list"])
        assert mock_run.call_args[0][0][0] == "gh"


def test_gh_run_no_capture():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        common.gh_run(["issue", "list"], capture=False)
        assert mock_run.call_args[1]["capture_output"] is False
