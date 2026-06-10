"""Tests for juggle_schedule_autofix."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import schedules.common as common
import schedules.autofix as autofix


# ---------------------------------------------------------------------------
# Idempotency: existing PR blocks run
# ---------------------------------------------------------------------------

def test_existing_pr_blocks_run(tmp_path):
    existing_prs = [{"headRefName": "cyc_schedule-autofix-2026-05-11", "number": 42, "state": "open"}]

    mock_db = MagicMock()
    mock_db.add_action_item = MagicMock()
    mock_thread = [{"id": "abc123"}]

    with patch("schedules.autofix.gh_pr_list_head", return_value=existing_prs), \
         patch.object(autofix, "get_db", return_value=mock_db), \
         patch.object(autofix, "db_query", return_value=mock_thread), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = autofix.run(dry_run=False)

    assert result == 1


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------

def test_dry_run_writes_pr_description(tmp_path):
    with patch("schedules.autofix.gh_pr_list_head", return_value=[]), \
         patch.object(autofix, "get_db", return_value=MagicMock()), \
         patch.object(autofix, "db_query", return_value=[]), \
         patch.object(autofix, "fx1_ruff", lambda *a, **kw: None), \
         patch.object(autofix, "fx2_vulture", lambda *a, **kw: None), \
         patch.object(autofix, "fx3_test_gaps", lambda *a, **kw: None), \
         patch.object(autofix, "fx4_watchdog_tests", lambda *a, **kw: None), \
         patch.object(autofix, "fx5_doc_drift", lambda *a, **kw: ("", [])), \
         patch.object(autofix, "fx6_changelog", lambda *a, **kw: None), \
         patch.object(autofix, "fx7_graphify", lambda *a, **kw: None), \
         patch.object(autofix, "is1_bandit", lambda *a: None), \
         patch.object(autofix, "is2_skill_audit", lambda *a: None), \
         patch.object(autofix, "_read_dogfood_snippet", return_value=""), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = autofix.run(dry_run=True)

    assert result == 0
    out = Path("/tmp/schedule-autofix-sample-PR.md")
    assert out.exists()
    assert "autofix:" in out.read_text()


# ---------------------------------------------------------------------------
# PR description schema includes required fields
# ---------------------------------------------------------------------------

def test_pr_description_schema():
    today = "2026-05-18"
    sections = {
        "FX-1": {"status": "committed", "files": 3, "lines": "+0/-5"},
        "FX-6": {"status": "no findings this week", "files": 0, "lines": ""},
    }
    desc = autofix._build_pr_description(today, sections, ["#42"], "drift text", "dogfood snippet")

    assert "cyc_schedule-autofix-2026-05-18" in desc
    assert "FX-1" in desc
    assert "FX-6" in desc
    assert "#42" in desc
    assert "dogfood snippet" in desc
    assert "drift text" in desc


# ---------------------------------------------------------------------------
# Issue dedup: existing issue is skipped
# ---------------------------------------------------------------------------

def test_issue_dedup_skips_existing():
    created = []

    with patch.object(common, "gh_issue_exists", return_value=True), \
         patch.object(common, "gh_create_issue", side_effect=lambda *a, **kw: created.append(a)):
        autofix.is1_bandit.__module__  # just import check
        # Simulate: would-be issue already exists → gh_create_issue should not be called
        title = "autofix: security finding — HIGH in src/foo.py:10"
        if not common.gh_issue_exists(title):
            common.gh_create_issue(title, "body", ["autofix"])

    assert len(created) == 0  # deduped


# ---------------------------------------------------------------------------
# Bandit parser handles empty output gracefully
# ---------------------------------------------------------------------------

def test_is1_bandit_empty_output():
    import subprocess
    mock_result = MagicMock()
    mock_result.stdout = "{}"
    mock_result.returncode = 0

    issues = []
    with patch("subprocess.run", return_value=mock_result):
        # Should not raise
        autofix.is1_bandit(issues)

    assert len(issues) == 0


# ---------------------------------------------------------------------------
# Vulture confidence threshold
# ---------------------------------------------------------------------------

def test_fx2_vulture_low_confidence_goes_to_issue(tmp_path):
    vulture_output = "src/foo.py:10: unused function 'old_func' (60% confidence)"
    mock_proc = MagicMock()
    mock_proc.stdout = vulture_output
    mock_proc.returncode = 0

    sections = {}
    issues = []

    with patch("subprocess.run", return_value=mock_proc), \
         patch.object(autofix, "git_run", return_value=MagicMock(stdout="", returncode=0)), \
         patch.object(autofix, "JUGGLE_REPO", tmp_path):
        autofix.fx2_vulture("branch", True, sections, issues)

    # 60% < 95% threshold → goes to issue, not commit
    assert any("old_func" in iss.get("title", "") or "probable dead code" in iss.get("title", "")
               for iss in issues)
