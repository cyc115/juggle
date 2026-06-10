"""Tests for juggle_schedule_reflect."""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import schedules.common as common
import schedules.reflect as reflect


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------

def test_dry_run_writes_digest(tmp_path):
    mock_db = MagicMock()

    with patch.object(reflect, "get_db", return_value=mock_db), \
         patch.object(reflect, "db_query", return_value=[]), \
         patch.object(common, "gh_pr_list_head", return_value=[]), \
         patch.object(reflect, "rf1_watchdog", lambda db, ct, s: s.update({"RF-1": "## Watchdog Health\n\nOK\n"})), \
         patch.object(reflect, "rf2_action_items", lambda db, ct, s: s.update({"RF-2": "## Action Item Fatigue\n\nOK\n"})), \
         patch.object(reflect, "rf3_completion_quality", lambda db, ct, s: s.update({"RF-3": "## Agent Output Quality\n\nOK\n"})), \
         patch.object(reflect, "rf4_context_bloat", lambda db, s: s.update({"RF-4": "## Context Bloat Candidates\n\nOK\n"})), \
         patch.object(reflect, "rf5_hindsight_lint", lambda ct, s: s.update({"RF-5": "## Memory Health\n\nOK\n"})), \
         patch.object(reflect, "rf6_auto_memory", lambda ct, s: s.update({"RF-6": "## Auto-Memory Contradictions\n\nOK\n"})), \
         patch.object(reflect, "rf7_skill_drift", lambda db, ct, s: s.update({"RF-7": "## Skill Drift\n\nOK\n"})), \
         patch.object(reflect, "rf8_dogfood_pulse", lambda s: s.update({"RF-8": "## Dogfood Pulse\n\nOK\n"})), \
         patch.object(reflect, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = reflect.run(dry_run=True)

    assert result == 0
    out = Path("/tmp/schedule-reflect-sample-digest.md")
    assert out.exists()
    content = out.read_text()
    assert "Juggle Weekly Digest" in content


# ---------------------------------------------------------------------------
# All 8 RF sections present in digest
# ---------------------------------------------------------------------------

def test_digest_contains_all_sections():
    today = "2026-05-18"
    sections = {
        "RF-1": "## Watchdog Health\n\nOK\n",
        "RF-2": "## Action Item Fatigue\n\nOK\n",
        "RF-3": "## Agent Output Quality\n\nOK\n",
        "RF-4": "## Context Bloat Candidates\n\nOK\n",
        "RF-5": "## Memory Health\n\nOK\n",
        "RF-6": "## Auto-Memory Contradictions\n\nOK\n",
        "RF-7": "## Skill Drift\n\nOK\n",
        "RF-8": "## Dogfood Pulse\n\nOK\n",
    }
    digest = reflect._build_digest(today, sections, "not run this week")

    for rf_id in ["RF-1", "RF-2", "RF-3", "RF-4", "RF-5", "RF-6", "RF-7", "RF-8"]:
        assert rf_id in digest or sections[rf_id].split("\n")[0].replace("## ", "") in digest


# ---------------------------------------------------------------------------
# Issue cap: max 5 filed
# ---------------------------------------------------------------------------

def test_issue_cap_5(tmp_path):
    sections = {
        f"RF-{i}": f"## Section {i}\n\nFinding\n"
        for i in range(1, 9)
    }
    filed = []

    def fake_create(title, body, labels=None, dry_run=False):
        filed.append(title)
        return f"https://github.com/issue/{len(filed)}"

    with patch.object(common, "gh_issue_exists", return_value=False), \
         patch.object(common, "gh_create_issue", fake_create), \
         patch.object(common, "_ensure_gh_label", return_value=None):
        today = "2026-05-18"
        report_path = tmp_path / f"reflect-{today}.md"
        reflect._file_reflect_issues(sections, today, report_path, dry_run=False)

    assert len(filed) <= reflect.MAX_ISSUES


# ---------------------------------------------------------------------------
# Idempotency: issue dedup
# ---------------------------------------------------------------------------

def test_issue_dedup_skips_existing(tmp_path):
    sections = {"RF-1": "## Watchdog Health\n\nFinding\n"}
    filed = []

    with patch.object(common, "gh_issue_exists", return_value=True), \
         patch.object(common, "gh_create_issue", lambda *a, **kw: filed.append(a)):
        today = "2026-05-18"
        report_path = tmp_path / f"reflect-{today}.md"
        reflect._file_reflect_issues(sections, today, report_path, dry_run=False)

    assert len(filed) == 0


# ---------------------------------------------------------------------------
# Partial section on cost cap
# ---------------------------------------------------------------------------

def test_cost_cap_in_rf1_continues_with_remaining(tmp_path):
    """Cost cap in RF-1 should not abort the entire routine — partial digest is written."""
    mock_db = MagicMock()
    rf1_calls = []
    rf8_calls = []

    def fake_rf1(db, ct, s):
        rf1_calls.append(1)
        raise common.CostCapExceeded("capped")

    def fake_rf8(s):
        rf8_calls.append(1)
        s["RF-8"] = "## Dogfood Pulse\n\nOK\n"

    with patch.object(reflect, "get_db", return_value=mock_db), \
         patch.object(common, "gh_pr_list_head", return_value=[]), \
         patch.object(reflect, "rf1_watchdog", fake_rf1), \
         patch.object(reflect, "rf2_action_items", lambda db, ct, s: None), \
         patch.object(reflect, "rf3_completion_quality", lambda db, ct, s: None), \
         patch.object(reflect, "rf4_context_bloat", lambda db, s: None), \
         patch.object(reflect, "rf5_hindsight_lint", lambda ct, s: None), \
         patch.object(reflect, "rf6_auto_memory", lambda ct, s: None), \
         patch.object(reflect, "rf7_skill_drift", lambda db, ct, s: None), \
         patch.object(reflect, "rf8_dogfood_pulse", fake_rf8), \
         patch.object(reflect, "_file_reflect_issues", return_value=[]), \
         patch.object(reflect, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path), \
         patch("schedules.common.git_commit", return_value=False), \
         patch("schedules.common.git_push", return_value=True):
        result = reflect.run(dry_run=True)

    # RF-8 should have run even though RF-1 raised CostCapExceeded
    assert len(rf8_calls) == 1


# ---------------------------------------------------------------------------
# RF-8 dogfood pulse reads most recent report
# ---------------------------------------------------------------------------

def test_rf8_dogfood_pulse_no_reports(tmp_path):
    sections = {}
    with patch.object(reflect, "REPORTS_DIR", tmp_path):
        reflect.rf8_dogfood_pulse(sections)

    assert "RF-8" in sections
    assert "No dogfood reports" in sections["RF-8"]


def test_rf8_dogfood_pulse_reads_latest(tmp_path):
    report = tmp_path / "dogfood-2026-05-18.md"
    report.write_text("# Juggle Self-Analysis\n\n## Suggested Improvements\n1. Fix watchdog stalls\n")

    sections = {}
    with patch.object(reflect, "REPORTS_DIR", tmp_path):
        reflect.rf8_dogfood_pulse(sections)

    assert "RF-8" in sections
    assert "dogfood-2026-05-18.md" in sections["RF-8"]
