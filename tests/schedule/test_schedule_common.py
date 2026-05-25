"""Tests for juggle_schedule_common shared infrastructure."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure src is on path
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
    with patch.object(common, "STATE_FILE", state_file):
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
    # 1M input @ $3 + 100k output @ $15*0.1 = $3 + $1.50 = $4.50
    assert 4.0 < cost < 5.0


def test_cost_tracker_estimate_haiku():
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    cost = ct.estimate_from_tokens(1_000_000, 100_000, model="claude-haiku-4-5")
    # 1M input @ $0.80 + 100k output @ $4*0.1 = $0.80 + $0.40 = $1.20
    assert 1.0 < cost < 1.5


# ---------------------------------------------------------------------------
# gh_issue_exists
# ---------------------------------------------------------------------------

def test_gh_issue_exists_false_on_subprocess_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("network error")):
        assert common.gh_issue_exists("some title") is False


def test_gh_issue_exists_false_when_no_match():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([{"title": "other title", "createdAt": "2026-05-18T00:00:00Z"}])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("some title") is False


def test_gh_issue_exists_true_when_match_within_window():
    mock_result = MagicMock()
    mock_result.stdout = json.dumps([
        {"title": "exact match", "createdAt": datetime.now(timezone.utc).isoformat()}
    ])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        assert common.gh_issue_exists("exact match", days=30) is True


# ---------------------------------------------------------------------------
# today_str / days_ago_iso
# ---------------------------------------------------------------------------

def test_today_str_format():
    s = common.today_str()
    parts = s.split("-")
    assert len(parts) == 3
    assert len(parts[0]) == 4  # YYYY


def test_days_ago_iso_is_past():
    ago = common.days_ago_iso(7)
    dt = datetime.fromisoformat(ago)
    now = datetime.now(timezone.utc)
    diff = (now - dt).total_seconds()
    assert 6.5 * 86400 < diff < 7.5 * 86400


# ---------------------------------------------------------------------------
# write_report
# ---------------------------------------------------------------------------

def test_write_report_dry_run_writes_to_tmp(tmp_path):
    out = tmp_path / "report.md"
    tmp = tmp_path / "tmp_report.md"
    result = common.write_report(out, "hello", dry_run=True, tmp_override=tmp)
    assert result == tmp
    assert tmp.read_text() == "hello"
    assert not out.exists()


def test_write_report_live_writes_to_path(tmp_path):
    out = tmp_path / "subdir" / "report.md"
    result = common.write_report(out, "world", dry_run=False)
    assert result == out
    assert out.read_text() == "world"


# ---------------------------------------------------------------------------
# gh_run
# ---------------------------------------------------------------------------

def test_gh_run_success():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "output"
    with patch("subprocess.run", return_value=mock_result):
        result = common.gh_run(["issue", "list"])
        assert result.stdout == "output"


def test_gh_run_fails_with_check_true():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "error"
    with patch("subprocess.run", side_effect=Exception("error")):
        with pytest.raises(Exception):
            common.gh_run(["issue", "list"], check=True)


def test_gh_run_ignores_failure_with_check_false():
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        result = common.gh_run(["issue", "list"], check=False)
        assert result.returncode == 1


def test_gh_run_calls_gh_prefix():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        common.gh_run(["issue", "list"])
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "gh"


# ---------------------------------------------------------------------------
# gh_create_issue
# ---------------------------------------------------------------------------

def test_gh_create_issue_dry_run_returns_none():
    result = common.gh_create_issue("title", "body", dry_run=True)
    assert result is None


def test_gh_create_issue_success():
    mock_result = MagicMock()
    mock_result.stdout = "https://github.com/owner/repo/issues/42"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_create_issue("test issue", "test body")
        assert result == "https://github.com/owner/repo/issues/42"


@pytest.mark.skip(reason="auto-generated, needs review")
def test_gh_create_issue_with_labels():
    mock_result = MagicMock()
    mock_result.stdout = "https://github.com/owner/repo/issues/42"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result), \
         patch("juggle_schedule_common._ensure_gh_label"):
        result = common.gh_create_issue("issue", "body", labels=["bug", "urgent"])
        assert result == "https://github.com/owner/repo/issues/42"


def test_gh_create_issue_handles_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("gh error")):
        result = common.gh_create_issue("title", "body")
        assert result is None


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
    mock_result.stdout = json.dumps([
        {"number": 42, "title": "fix bug", "state": "OPEN", "headRefName": "cyc_fix"}
    ])
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_pr_list_head("cyc_fix")
        assert len(result) == 1
        assert result[0]["number"] == 42


def test_gh_pr_list_head_empty():
    mock_result = MagicMock()
    mock_result.stdout = "[]"
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_pr_list_head("nonexistent")
        assert result == []


def test_gh_pr_list_head_handles_error():
    with patch("juggle_schedule_common.gh_run", side_effect=Exception("gh error")):
        result = common.gh_pr_list_head("cyc_fix")
        assert result == []


def test_gh_pr_list_head_empty_stdout():
    mock_result = MagicMock()
    mock_result.stdout = None
    with patch("juggle_schedule_common.gh_run", return_value=mock_result):
        result = common.gh_pr_list_head("cyc_fix")
        assert result == []


# ---------------------------------------------------------------------------
# claude_p
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="auto-generated, needs review")
def test_claude_p_success():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({
        "result": "hello world",
        "usage": {"input_tokens": 10, "output_tokens": 5}
    })
    with patch("subprocess.run", return_value=mock_result):
        result = common.claude_p("test prompt")
        assert result == "hello world"


@pytest.mark.skip(reason="auto-generated, needs review")
def test_claude_p_with_cost_tracker():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({
        "result": "output",
        "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000}
    })
    ct = common.CostTracker(cap_usd=10.0, routine="test")
    with patch("subprocess.run", return_value=mock_result):
        result = common.claude_p("test", cost_tracker=ct)
        assert result == "output"
        assert ct.total > 0


@pytest.mark.skip(reason="auto-generated, needs review")
def test_claude_p_fallback_to_text():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "plain text output"
    with patch("subprocess.run", return_value=mock_result):
        result = common.claude_p("test prompt")
        assert result == "plain text output"


def test_claude_p_handles_error():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "error"
    with patch("subprocess.run", return_value=mock_result):
        result = common.claude_p("test prompt")
        assert result == ""


@pytest.mark.skip(reason="auto-generated, needs review")
def test_claude_p_with_custom_model():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "test"})
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        common.claude_p("test", model="claude-haiku-4-5")
        call_args = mock_run.call_args[0][0]
        assert "claude-haiku-4-5" in call_args


@pytest.mark.skip(reason="auto-generated, needs review")
def test_claude_p_timeout_param():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = json.dumps({"result": "test"})
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        common.claude_p("test", timeout=300)
        assert mock_run.call_args[1]["timeout"] == 300


# ---------------------------------------------------------------------------
# get_db
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="auto-generated, needs review")
def test_get_db_returns_juggle_db_instance():
    result = common.get_db()
    assert result is not None
    assert hasattr(result, "_connect")


@pytest.mark.skip(reason="auto-generated, needs review")
def test_get_db_uses_test_db_env_var(tmp_path, monkeypatch):
    test_db = tmp_path / "test.db"
    monkeypatch.setenv("_JUGGLE_TEST_DB", str(test_db))
    result = common.get_db()
    assert result is not None
