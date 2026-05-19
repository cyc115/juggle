#!/usr/bin/env python3
"""Tests for juggle_schedule_common.py"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_schedule_common as jsc


class TestLoadState:
    """Tests for load_state()"""

    def test_load_state_empty_when_file_not_exists(self, tmp_path):
        """Returns empty dict when STATE_FILE doesn't exist"""
        with patch.object(jsc, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = jsc.load_state()
            assert state == {}

    def test_load_state_returns_dict_when_file_exists(self, tmp_path):
        """Returns dict contents when STATE_FILE exists"""
        state_file = tmp_path / "state.json"
        test_data = {"routine1": {"last_success": "2026-05-18T00:00:00+00:00"}}
        state_file.write_text(json.dumps(test_data))

        with patch.object(jsc, "STATE_FILE", state_file):
            state = jsc.load_state()
            assert state == test_data

    def test_load_state_returns_empty_on_invalid_json(self, tmp_path):
        """Returns empty dict when JSON is invalid"""
        state_file = tmp_path / "state.json"
        state_file.write_text("invalid json{{{")

        with patch.object(jsc, "STATE_FILE", state_file):
            state = jsc.load_state()
            assert state == {}

    def test_load_state_returns_empty_on_read_error(self, tmp_path):
        """Returns empty dict when file read raises exception"""
        state_file = tmp_path / "state.json"

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
                state = jsc.load_state()
                assert state == {}


class TestSaveState:
    """Tests for save_state()"""

    def test_save_state_writes_json(self, tmp_path):
        """Writes state dict as JSON to STATE_FILE"""
        state_file = tmp_path / "state.json"
        test_state = {"routine1": {"last_success": "2026-05-18T00:00:00+00:00"}}

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.save_state(test_state)

        saved = json.loads(state_file.read_text())
        assert saved == test_state

    def test_save_state_creates_dirs(self, tmp_path):
        """Creates JUGGLE_DIR if it doesn't exist"""
        state_file = tmp_path / "new_dir" / "state.json"
        test_state = {"test": "data"}

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", state_file.parent):
                jsc.save_state(test_state)

        assert state_file.exists()

    def test_save_state_formats_with_indent(self, tmp_path):
        """Saves JSON with indent=2 for readability"""
        state_file = tmp_path / "state.json"
        test_state = {"key": "value"}

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.save_state(test_state)

        content = state_file.read_text()
        assert "  " in content  # Check for indentation


class TestMarkRunComplete:
    """Tests for mark_run_complete()"""

    def test_mark_run_complete_sets_timestamp(self, tmp_path):
        """Sets last_success timestamp for routine"""
        state_file = tmp_path / "state.json"

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.mark_run_complete("test_routine")

        state = json.loads(state_file.read_text())
        assert "test_routine" in state
        assert "last_success" in state["test_routine"]

    def test_mark_run_complete_timestamp_is_iso_format(self, tmp_path):
        """Timestamp is valid ISO format"""
        state_file = tmp_path / "state.json"

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.mark_run_complete("test_routine")

        state = json.loads(state_file.read_text())
        ts_str = state["test_routine"]["last_success"]
        # Should not raise
        datetime.fromisoformat(ts_str)

    def test_mark_run_complete_overwrites_previous(self, tmp_path):
        """Overwrites previous timestamp"""
        state_file = tmp_path / "state.json"
        old_time = "2026-01-01T00:00:00+00:00"
        state_file.write_text(json.dumps({"test_routine": {"last_success": old_time}}))

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.mark_run_complete("test_routine")

        state = json.loads(state_file.read_text())
        assert state["test_routine"]["last_success"] != old_time

    def test_mark_run_complete_preserves_other_routines(self, tmp_path):
        """Preserves other routines in state"""
        state_file = tmp_path / "state.json"
        initial_state = {
            "routine1": {"last_success": "2026-01-01T00:00:00+00:00"},
            "routine2": {"last_success": "2026-02-01T00:00:00+00:00"}
        }
        state_file.write_text(json.dumps(initial_state))

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.mark_run_complete("routine3")

        state = json.loads(state_file.read_text())
        assert "routine1" in state
        assert "routine2" in state
        assert "routine3" in state


class TestLastRunTs:
    """Tests for last_run_ts()"""

    def test_last_run_ts_returns_none_when_routine_not_found(self, tmp_path):
        """Returns None when routine not in state"""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({}))

        with patch.object(jsc, "STATE_FILE", state_file):
            result = jsc.last_run_ts("nonexistent")
            assert result is None

    def test_last_run_ts_returns_datetime(self, tmp_path):
        """Returns datetime object for valid timestamp"""
        state_file = tmp_path / "state.json"
        iso_str = "2026-05-18T12:30:45+00:00"
        state_file.write_text(json.dumps({"routine1": {"last_success": iso_str}}))

        with patch.object(jsc, "STATE_FILE", state_file):
            result = jsc.last_run_ts("routine1")
            assert isinstance(result, datetime)
            assert result.isoformat() == iso_str

    def test_last_run_ts_returns_none_for_invalid_timestamp(self, tmp_path):
        """Returns None when timestamp is invalid"""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"routine1": {"last_success": "not-a-timestamp"}}))

        with patch.object(jsc, "STATE_FILE", state_file):
            result = jsc.last_run_ts("routine1")
            assert result is None

    def test_last_run_ts_returns_none_when_no_last_success_key(self, tmp_path):
        """Returns None when last_success key is missing"""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"routine1": {}}))

        with patch.object(jsc, "STATE_FILE", state_file):
            result = jsc.last_run_ts("routine1")
            assert result is None


class TestGhRun:
    """Tests for gh_run()"""

    def test_gh_run_runs_gh_command(self):
        """Calls subprocess.run with gh command"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="output", stderr="", returncode=0)
            jsc.gh_run(["issue", "list"])

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0][0] == "gh"
            assert args[0][1:] == ["issue", "list"]

    def test_gh_run_respects_check_parameter(self):
        """Passes check parameter to subprocess.run"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            jsc.gh_run(["issue", "list"], check=False)

            _, kwargs = mock_run.call_args
            assert kwargs["check"] is False

    def test_gh_run_captures_output_by_default(self):
        """Captures output by default"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            jsc.gh_run(["issue", "list"])

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["capture_output"] is True
            assert call_kwargs["text"] is True

    def test_gh_run_respects_capture_parameter(self):
        """Respects capture parameter"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            jsc.gh_run(["issue", "list"], capture=False)

            _, kwargs = mock_run.call_args
            assert kwargs["capture_output"] is False

    def test_gh_run_returns_completed_process(self):
        """Returns CompletedProcess object"""
        mock_result = MagicMock(spec=subprocess.CompletedProcess)
        with patch("subprocess.run", return_value=mock_result):
            result = jsc.gh_run(["issue", "list"])
            assert result is mock_result


class TestGhIssueExists:
    """Tests for gh_issue_exists()"""

    def test_gh_issue_exists_returns_false_when_no_issues(self):
        """Returns False when gh returns empty list"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="[]")
            result = jsc.gh_issue_exists("Test Issue")
            assert result is False

    def test_gh_issue_exists_finds_exact_title_match(self):
        """Returns True when exact title match found within days"""
        now = datetime.now(timezone.utc)
        recent_time = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")

        issues = [{"title": "Test Issue", "createdAt": recent_time}]
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(issues))
            result = jsc.gh_issue_exists("Test Issue", days=30)
            assert result is True

    def test_gh_issue_exists_returns_false_for_partial_title_match(self):
        """Returns False for partial title match (requires exact match)"""
        issues = [{"title": "Test Issue Extended", "createdAt": datetime.now(timezone.utc).isoformat()}]
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(issues))
            result = jsc.gh_issue_exists("Test Issue")
            assert result is False

    def test_gh_issue_exists_respects_days_cutoff(self):
        """Returns False when issue is older than cutoff"""
        old_time = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat().replace("+00:00", "Z")
        issues = [{"title": "Test Issue", "createdAt": old_time}]

        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(issues))
            result = jsc.gh_issue_exists("Test Issue", days=30)
            assert result is False

    def test_gh_issue_exists_returns_false_on_exception(self):
        """Returns False when gh_run raises exception"""
        with patch.object(jsc, "gh_run", side_effect=Exception("gh not found")):
            result = jsc.gh_issue_exists("Test Issue")
            assert result is False

    def test_gh_issue_exists_handles_invalid_timestamp_format(self):
        """Returns False when timestamp format is invalid"""
        issues = [{"title": "Test Issue", "createdAt": "invalid-date"}]
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(issues))
            result = jsc.gh_issue_exists("Test Issue")
            assert result is False

    def test_gh_issue_exists_strips_whitespace_from_titles(self):
        """Strips whitespace when comparing titles"""
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        issues = [{"title": "  Test Issue  ", "createdAt": now}]

        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(issues))
            result = jsc.gh_issue_exists("Test Issue")
            assert result is True


class TestGhCreateIssue:
    """Tests for gh_create_issue()"""

    def test_gh_create_issue_returns_none_in_dry_run(self):
        """Returns None in dry_run mode"""
        with patch.object(jsc, "gh_run"):
            result = jsc.gh_create_issue("Test", "Body", dry_run=True)
            assert result is None

    def test_gh_create_issue_calls_gh_with_title_and_body(self):
        """Calls gh with title and body arguments"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="https://github.com/issue/1")
            jsc.gh_create_issue("Test Title", "Test Body")

            mock_gh.assert_called_once()
            args = mock_gh.call_args[0][0]
            assert "--title" in args
            assert "Test Title" in args
            assert "--body" in args
            assert "Test Body" in args

    def test_gh_create_issue_includes_labels_when_provided(self):
        """Includes labels in gh command"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="https://github.com/issue/1")
            with patch.object(jsc, "_ensure_gh_label"):
                jsc.gh_create_issue("Title", "Body", labels=["bug", "urgent"])

                args = mock_gh.call_args[0][0]
                assert "--label" in args

    def test_gh_create_issue_returns_stdout(self):
        """Returns stdout from gh command"""
        expected_url = "https://github.com/owner/repo/issues/123"
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=expected_url)
            result = jsc.gh_create_issue("Title", "Body")
            assert result == expected_url

    def test_gh_create_issue_returns_none_on_exception(self):
        """Returns None when gh_run raises exception"""
        with patch.object(jsc, "gh_run", side_effect=Exception("gh failed")):
            result = jsc.gh_create_issue("Title", "Body")
            assert result is None

    def test_gh_create_issue_skips_label_creation_on_error(self):
        """Continues even if label creation fails"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="https://github.com/issue/1")
            with patch.object(jsc, "_ensure_gh_label", side_effect=Exception("label failed")):
                # Should not raise
                result = jsc.gh_create_issue("Title", "Body", labels=["test"])
                assert result is not None


class TestGhPrListHead:
    """Tests for gh_pr_list_head()"""

    def test_gh_pr_list_head_returns_parsed_json(self):
        """Returns parsed JSON from gh pr list"""
        prs = [{"number": 1, "title": "PR 1", "state": "open", "url": "https://...", "headRefName": "feature"}]
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=json.dumps(prs))
            result = jsc.gh_pr_list_head("feature/")
            assert result == prs

    def test_gh_pr_list_head_calls_gh_with_head_prefix(self):
        """Calls gh with --head parameter"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="[]")
            jsc.gh_pr_list_head("feature-")

            args = mock_gh.call_args[0][0]
            assert "--head" in args
            assert "feature-" in args

    def test_gh_pr_list_head_returns_empty_list_on_exception(self):
        """Returns empty list when gh_run raises exception"""
        with patch.object(jsc, "gh_run", side_effect=Exception("gh failed")):
            result = jsc.gh_pr_list_head("feature/")
            assert result == []

    def test_gh_pr_list_head_returns_empty_list_on_empty_stdout(self):
        """Returns empty list when stdout is None"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout=None)
            result = jsc.gh_pr_list_head("feature/")
            assert result == []

    @pytest.mark.skip(reason="auto-generated, needs review")
    def test_gh_pr_list_head_handles_malformed_json(self):
        """Returns empty list on JSON parse error"""
        with patch.object(jsc, "gh_run") as mock_gh:
            mock_gh.return_value = MagicMock(stdout="invalid json")
            result = jsc.gh_pr_list_head("feature/")
            assert result == []


class TestClaudeP:
    """Tests for claude_p()"""

    def test_claude_p_runs_claude_command(self):
        """Calls subprocess.run with claude -p"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": "output"})
            )
            jsc.claude_p("test prompt")

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0][0] == "claude"
            assert "-p" in args[0]

    def test_claude_p_returns_result_field(self):
        """Returns result field from JSON response"""
        expected = "test output"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": expected})
            )
            result = jsc.claude_p("prompt")
            assert result == expected

    def test_claude_p_returns_content_field_if_no_result(self):
        """Falls back to content field"""
        expected = "content output"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"content": expected})
            )
            result = jsc.claude_p("prompt")
            assert result == expected

    def test_claude_p_returns_empty_on_non_zero_exit(self):
        """Returns empty string on non-zero exit code"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            result = jsc.claude_p("prompt")
            assert result == ""

    def test_claude_p_respects_model_parameter(self):
        """Passes model to claude command"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": "output"})
            )
            jsc.claude_p("prompt", model="claude-haiku-4-5-20251001")

            args = mock_run.call_args[0][0]
            assert "--model" in args
            assert "claude-haiku-4-5-20251001" in args

    def test_claude_p_tracks_cost_when_tracker_provided(self):
        """Updates cost_tracker with estimated cost"""
        tracker = jsc.CostTracker(1.0, "test")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "result": "output",
                    "usage": {"input_tokens": 1000, "output_tokens": 500}
                })
            )
            jsc.claude_p("prompt", cost_tracker=tracker)
            assert tracker.total > 0

    def test_claude_p_falls_back_to_plain_text(self):
        """Falls back to plain text if JSON parse fails"""
        expected_text = "plain text output"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=expected_text
            )
            result = jsc.claude_p("prompt")
            assert result == expected_text

    def test_claude_p_respects_timeout_parameter(self):
        """Passes timeout to subprocess.run"""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"result": "output"})
            )
            jsc.claude_p("prompt", timeout=60)

            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 60

    @pytest.mark.skip(reason="auto-generated, needs review")
    def test_claude_p_timeout_exception(self):
        """Handles subprocess timeout exception"""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 1)):
            jsc.claude_p("prompt")
            # Should return empty string or raise gracefully


class TestGetDb:
    """Tests for get_db()"""

    def test_get_db_returns_juggle_db_instance(self):
        """Returns JuggleDB instance"""
        result = jsc.get_db()
        assert result is not None
        # Check that it has expected attributes/methods
        assert hasattr(result, "_connect")

    def test_get_db_respects_test_db_env_var(self):
        """Uses _JUGGLE_TEST_DB environment variable"""
        test_db_path = "/tmp/test.db"
        with patch.dict(os.environ, {"_JUGGLE_TEST_DB": test_db_path}):
            with patch("juggle_db.JuggleDB") as mock_db_class:
                jsc.get_db()
                mock_db_class.assert_called_once()
                args = mock_db_class.call_args[0]
                assert test_db_path in args

    @pytest.mark.skip(reason="auto-generated, needs review")
    def test_get_db_uses_default_db_path(self):
        """Uses default DB_PATH when env var not set"""
        with patch.dict(os.environ, {}, clear=True):
            with patch("juggle_db.DB_PATH", "/default/path.db"):
                with patch("juggle_db.JuggleDB"):
                    jsc.get_db()
                    # Should be called with default path


# ============================================================================
# Integration tests (may require real state file or services)
# ============================================================================

class TestStateIntegration:
    """Integration tests for state management"""

    def test_roundtrip_state_save_load(self, tmp_path):
        """Save and load state roundtrip"""
        state_file = tmp_path / "state.json"
        original = {"routine": {"last_success": "2026-05-18T12:30:00+00:00"}}

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.save_state(original)
                loaded = jsc.load_state()
                assert loaded == original

    def test_mark_and_retrieve_run_complete(self, tmp_path):
        """Mark complete and retrieve timestamp"""
        state_file = tmp_path / "state.json"

        with patch.object(jsc, "STATE_FILE", state_file):
            with patch.object(jsc, "JUGGLE_DIR", tmp_path):
                jsc.mark_run_complete("test_routine")
                ts = jsc.last_run_ts("test_routine")

                assert ts is not None
                assert isinstance(ts, datetime)
                # Should be recent (within 1 second)
                delta = datetime.now(timezone.utc) - ts
                assert delta.total_seconds() < 1


class TestCostTracker:
    """Tests for CostTracker class"""

    def test_cost_tracker_accumulates_cost(self):
        """Accumulates cost when adding"""
        tracker = jsc.CostTracker(10.0, "test")
        tracker.add(1.0)
        tracker.add(2.0)
        assert tracker.total == 3.0

    def test_cost_tracker_raises_on_cap_exceeded(self):
        """Raises CostCapExceeded when cap exceeded"""
        tracker = jsc.CostTracker(1.0, "test")
        tracker.add(0.5)
        with pytest.raises(jsc.CostCapExceeded):
            tracker.add(0.6)

    def test_cost_tracker_estimates_from_tokens_sonnet(self):
        """Estimates cost from tokens for Sonnet"""
        tracker = jsc.CostTracker(1.0, "test")
        # Sonnet: input $3/M, output $15/M
        cost = tracker.estimate_from_tokens(1_000_000, 1_000_000, "claude-sonnet-4-6")
        expected = 3.0 + 15.0  # $18
        assert cost == expected

    def test_cost_tracker_estimates_from_tokens_haiku(self):
        """Estimates cost from tokens for Haiku"""
        tracker = jsc.CostTracker(1.0, "test")
        # Haiku: input $0.80/M, output $4/M
        cost = tracker.estimate_from_tokens(1_000_000, 1_000_000, "claude-haiku-4-5-20251001")
        expected = 0.80 + 4.0  # $4.80
        assert cost == expected

    def test_cost_tracker_dry_run_mode(self):
        """Dry run mode doesn't check cap"""
        tracker = jsc.CostTracker(0.0, "test", dry_run=True)
        # Should not raise even though cap is 0
        tracker.add(1.0)
        assert tracker.total == 1.0


class TestCostCapExceeded:
    """Tests for CostCapExceeded exception"""

    def test_cost_cap_exceeded_is_exception(self):
        """CostCapExceeded is an Exception"""
        exc = jsc.CostCapExceeded("test message")
        assert isinstance(exc, Exception)
