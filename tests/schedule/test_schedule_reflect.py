"""Tests for juggle_schedule_reflect."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import juggle_schedule_common as common
import juggle_schedule_reflect as reflect


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------

def test_dry_run_writes_digest(tmp_path):
    mock_db = MagicMock()
    with patch.object(reflect, "get_db", return_value=mock_db), \
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
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = reflect.run(dry_run=True)

    assert result == 0
    out = Path("/tmp/schedule-reflect-sample-digest.md")
    assert out.exists()
    assert "Juggle Weekly Digest" in out.read_text()


# ---------------------------------------------------------------------------
# All 8 RF sections in digest
# ---------------------------------------------------------------------------

def test_digest_contains_all_sections():
    today = "2026-05-18"
    sections = {f"RF-{i}": f"## Section {i}\n\nOK\n" for i in range(1, 9)}
    digest = reflect._build_digest(today, sections, "not run this week")
    for i in range(1, 9):
        assert f"RF-{i}" in digest or f"Section {i}" in digest


def test_digest_missing_section_shows_not_run():
    today = "2026-05-18"
    sections = {"RF-1": "## Watchdog Health\n\nOK\n"}
    digest = reflect._build_digest(today, sections, "not run")
    assert "*Not run.*" in digest


# ---------------------------------------------------------------------------
# Issue cap: max 5
# ---------------------------------------------------------------------------

def test_issue_cap_5(tmp_path):
    sections = {f"RF-{i}": f"## Section {i}\n\nFinding with detail here.\n" for i in range(1, 9)}
    filed = []

    def fake_create(title, body, labels=None, dry_run=False):
        filed.append(title)
        return f"https://github.com/issue/{len(filed)}"

    with patch.object(common, "gh_issue_exists", return_value=False), \
         patch.object(common, "gh_create_issue", side_effect=fake_create):
        reflect._file_reflect_issues(sections, "2026-05-18", tmp_path / "r.md", dry_run=False)

    assert len(filed) <= reflect.MAX_ISSUES


# ---------------------------------------------------------------------------
# Issue dedup: existing issue is skipped
# ---------------------------------------------------------------------------

def test_issue_dedup_skips_existing(tmp_path):
    sections = {"RF-1": "## Watchdog Health\n\nCritical finding here.\n"}
    filed = []

    with patch.object(common, "gh_issue_exists", return_value=True), \
         patch.object(common, "gh_create_issue", side_effect=lambda *a, **kw: filed.append(a)):
        reflect._file_reflect_issues(sections, "2026-05-18", tmp_path / "r.md", dry_run=False)

    assert len(filed) == 0


# ---------------------------------------------------------------------------
# RF-4: context bloat — no messages table is handled
# ---------------------------------------------------------------------------

def test_rf4_no_messages_table():
    mock_db = MagicMock()
    sections = {}
    with patch.object(reflect, "db_query", side_effect=Exception("no such table: messages")):
        reflect.rf4_context_bloat(mock_db, sections)
    assert "RF-4" in sections
    assert "not available" in sections["RF-4"].lower() or "Messages" in sections["RF-4"]


# ---------------------------------------------------------------------------
# RF-8: dogfood pulse with no reports
# ---------------------------------------------------------------------------

def test_rf8_no_reports(tmp_path):
    sections = {}
    with patch.object(reflect, "REPORTS_DIR", tmp_path):
        reflect.rf8_dogfood_pulse(sections)
    assert "RF-8" in sections
    assert "No dogfood reports" in sections["RF-8"]


def test_rf8_reads_latest_report(tmp_path):
    report = tmp_path / "dogfood-2026-05-18.md"
    report.write_text("## Suggested Improvements\n1. Optimize dispatch\n")
    sections = {}
    with patch.object(reflect, "REPORTS_DIR", tmp_path):
        reflect.rf8_dogfood_pulse(sections)
    assert "dogfood-2026-05-18.md" in sections["RF-8"]


# ---------------------------------------------------------------------------
# Cost cap stops further sections
# ---------------------------------------------------------------------------

def test_cost_cap_produces_partial_digest(tmp_path):
    mock_db = MagicMock()

    def rf1_exceeds(db, ct, s):
        raise common.CostCapExceeded("cap exceeded at RF-1")

    with patch.object(reflect, "get_db", return_value=mock_db), \
         patch.object(common, "gh_pr_list_head", return_value=[]), \
         patch.object(reflect, "rf1_watchdog", rf1_exceeds), \
         patch.object(reflect, "REPORTS_DIR", tmp_path), \
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = reflect.run(dry_run=True)

    assert result == 0
    out = Path("/tmp/schedule-reflect-sample-digest.md")
    content = out.read_text()
    assert "COST CAP" in content or "*Not run.*" in content


# ---------------------------------------------------------------------------
# Autofix PR cross-link
# ---------------------------------------------------------------------------

def test_find_autofix_pr_ref_not_run():
    with patch.object(reflect, "gh_pr_list_head", return_value=[]):
        ref = reflect._find_autofix_pr_ref()
    assert "not run" in ref


def test_find_autofix_pr_ref_with_pr():
    with patch.object(reflect, "gh_pr_list_head", return_value=[{"number": 99, "state": "OPEN"}]):
        ref = reflect._find_autofix_pr_ref()
    assert "99" in ref


# ---------------------------------------------------------------------------
# Bug 2 regression: cost cap is per-section, explicit skip markers, no cascade
# ---------------------------------------------------------------------------

def test_reflect_cost_cap_explicit_skip_markers_not_silent(tmp_path):
    """After RF-1 exhausts budget, remaining sections show explicit skip marker — not '*Not run.*'."""
    mock_db = MagicMock()

    def rf1_exceeds_cap(db, ct, s):
        s["RF-1"] = "## RF-1\n\nWatchdog OK\n"
        # Force tracker total above cap so pre-check triggers for RF-2..RF-8
        ct._total = reflect.COST_CAP + 1.0
        raise common.CostCapExceeded("cap exceeded during RF-1")

    with patch.object(reflect, "get_db", return_value=mock_db), \
         patch.object(common, "gh_pr_list_head", return_value=[]), \
         patch.object(reflect, "rf1_watchdog", rf1_exceeds_cap), \
         patch.object(reflect, "REPORTS_DIR", tmp_path), \
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = reflect.run(dry_run=True)

    assert result == 0
    content = Path("/tmp/schedule-reflect-sample-digest.md").read_text()

    # RF-1 must appear in digest
    assert "Watchdog OK" in content, "RF-1 content missing"

    # Skipped sections must have explicit marker, not silent *Not run.*
    assert "[COST CAP REACHED — SKIPPED]" in content, \
        "Expected explicit skip marker but not found"


def test_reflect_cost_cap_dry_run_does_not_raise(tmp_path):
    """In dry_run mode, cost tracker must not raise CostCapExceeded — sections can run freely."""
    mock_db = MagicMock()
    sections_run = []

    def rf_expensive(key):
        def fn(*args, **kwargs):
            sections_run.append(key)
            # Find cost tracker and add cost
            for arg in args:
                if isinstance(arg, common.CostTracker):
                    arg.add(0.50)  # Each section costs $0.50; cap is $2.00
                    break
            # Update sections dict
            for arg in args:
                if isinstance(arg, dict):
                    arg[key] = f"## {key}\n\nOK\n"
                    break
        return fn

    with patch.object(reflect, "get_db", return_value=mock_db), \
         patch.object(common, "gh_pr_list_head", return_value=[]), \
         patch.object(reflect, "rf1_watchdog", rf_expensive("RF-1")), \
         patch.object(reflect, "rf2_action_items", rf_expensive("RF-2")), \
         patch.object(reflect, "rf3_completion_quality", rf_expensive("RF-3")), \
         patch.object(reflect, "rf4_context_bloat", lambda db, s: s.update({"RF-4": "## RF-4\n\nOK\n"})), \
         patch.object(reflect, "rf5_hindsight_lint", rf_expensive("RF-5")), \
         patch.object(reflect, "rf6_auto_memory", rf_expensive("RF-6")), \
         patch.object(reflect, "rf7_skill_drift", rf_expensive("RF-7")), \
         patch.object(reflect, "rf8_dogfood_pulse", lambda s: s.update({"RF-8": "## RF-8\n\nOK\n"})), \
         patch.object(reflect, "REPORTS_DIR", tmp_path), \
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = reflect.run(dry_run=True)  # Must not raise even with $3.50 total cost

    assert result == 0
