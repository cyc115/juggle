"""Tests for juggle_schedule_autofix."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import juggle_schedule_common as common
import juggle_schedule_autofix as autofix


# ---------------------------------------------------------------------------
# Idempotency: existing PR blocks run
# ---------------------------------------------------------------------------


def test_existing_pr_blocks_run(tmp_path):
    existing_prs = [
        {
            "headRefName": "cyc_schedule-autofix-2026-05-11",
            "number": 42,
            "state": "open",
        }
    ]
    mock_db = MagicMock()
    mock_db.add_action_item = MagicMock()

    with (
        patch("juggle_schedule_autofix.gh_pr_list_head", return_value=existing_prs),
        patch.object(autofix, "get_db", return_value=mock_db),
        patch.object(autofix, "db_query", return_value=[{"id": "abc123"}]),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = autofix.run(dry_run=False)

    assert result == 1


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------


def test_dry_run_writes_pr_description(tmp_path):
    with (
        patch("juggle_schedule_autofix.gh_pr_list_head", return_value=[]),
        patch.object(autofix, "get_db", return_value=MagicMock()),
        patch.object(autofix, "db_query", return_value=[]),
        patch.object(autofix, "fx1_ruff", lambda *a, **kw: None),
        patch.object(autofix, "fx2_vulture", lambda *a, **kw: None),
        patch.object(autofix, "fx3_test_gaps", lambda *a, **kw: None),
        patch.object(autofix, "fx4_watchdog_tests", lambda *a, **kw: None),
        patch.object(autofix, "fx5_doc_drift", lambda *a, **kw: ("", [])),
        patch.object(autofix, "fx6_changelog", lambda *a, **kw: None),
        patch.object(autofix, "fx7_graphify", lambda *a, **kw: None),
        patch.object(autofix, "is1_bandit", lambda *a: None),
        patch.object(autofix, "is2_skill_audit", lambda *a: None),
        patch.object(autofix, "_read_dogfood_snippet", return_value=""),
        patch(
            "juggle_schedule_autofix.git_run",
            return_value=MagicMock(returncode=0, stdout=""),
        ),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = autofix.run(dry_run=True)

    assert result == 0
    out = Path("/tmp/schedule-autofix-sample-PR.md")
    assert out.exists()
    content = out.read_text()
    assert "autofix:" in content


# ---------------------------------------------------------------------------
# PR description schema
# ---------------------------------------------------------------------------


def test_pr_description_schema(tmp_path):
    with patch.object(autofix, "REPORTS_DIR", tmp_path):
        today = "2026-05-18"
        sections = {
            "FX-1": {"status": "committed", "files": 3, "lines": "+0/-5"},
            "FX-6": {"status": "no findings this week", "files": 0, "lines": ""},
        }
        desc = autofix._build_pr_description(
            today, sections, ["#42"], "drift text", "dogfood snippet"
        )

    assert "cyc_schedule-autofix-2026-05-18" in desc
    assert "FX-1" in desc
    assert "FX-6" in desc
    assert "#42" in desc
    assert "dogfood snippet" in desc
    assert "drift text" in desc


# ---------------------------------------------------------------------------
# Issue dedup
# ---------------------------------------------------------------------------


def test_issue_dedup_skips_existing():
    created = []
    with (
        patch.object(common, "gh_issue_exists", return_value=True),
        patch.object(
            common, "gh_create_issue", side_effect=lambda *a, **kw: created.append(a)
        ),
    ):
        title = "autofix: security finding — HIGH in src/foo.py:10"
        if not common.gh_issue_exists(title):
            common.gh_create_issue(title, "body", ["autofix"])
    assert len(created) == 0


# ---------------------------------------------------------------------------
# Dry run — no git branch created
# ---------------------------------------------------------------------------


def test_dry_run_does_not_create_branch(tmp_path):
    git_calls = []

    def fake_git_run(args, **kwargs):
        git_calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("juggle_schedule_autofix.gh_pr_list_head", return_value=[]),
        patch.object(autofix, "get_db", return_value=MagicMock()),
        patch.object(autofix, "db_query", return_value=[]),
        patch.object(autofix, "fx1_ruff", lambda *a, **kw: None),
        patch.object(autofix, "fx2_vulture", lambda *a, **kw: None),
        patch.object(autofix, "fx3_test_gaps", lambda *a, **kw: None),
        patch.object(autofix, "fx4_watchdog_tests", lambda *a, **kw: None),
        patch.object(autofix, "fx5_doc_drift", lambda *a, **kw: ("", [])),
        patch.object(autofix, "fx6_changelog", lambda *a, **kw: None),
        patch.object(autofix, "fx7_graphify", lambda *a, **kw: None),
        patch.object(autofix, "is1_bandit", lambda *a: None),
        patch.object(autofix, "is2_skill_audit", lambda *a: None),
        patch.object(autofix, "_read_dogfood_snippet", return_value=""),
        patch("juggle_schedule_autofix.git_run", side_effect=fake_git_run),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        autofix.run(dry_run=True)

    branch_calls = [c for c in git_calls if "checkout" in c and "-b" in c]
    assert len(branch_calls) == 0


# ---------------------------------------------------------------------------
# dogfood snippet — skips if report >48h old
# ---------------------------------------------------------------------------


def test_read_dogfood_snippet_skips_stale_report(tmp_path):
    old_report = tmp_path / "dogfood-2026-05-01.md"
    old_report.write_text("## Suggested Improvements\n1. Fix something important\n")
    import os

    # Set mtime to 3 days ago
    old_time = __import__("time").time() - 3 * 86400
    os.utime(str(old_report), (old_time, old_time))

    with patch.object(autofix, "REPORTS_DIR", tmp_path):
        snippet = autofix._read_dogfood_snippet()

    assert snippet == ""


def test_read_dogfood_snippet_reads_recent_report(tmp_path):
    report = tmp_path / "dogfood-2026-05-18.md"
    report.write_text(
        "## Suggested Improvements\n1. Add better error handling in juggle_hooks.py:42\n"
    )

    with patch.object(autofix, "REPORTS_DIR", tmp_path):
        snippet = autofix._read_dogfood_snippet()

    assert len(snippet) > 0


# ---------------------------------------------------------------------------
# Bug 1 regression: FX-1 scoped to changed-files-this-week, not entire src/
# ---------------------------------------------------------------------------


def test_fx1_ruff_scoped_to_changed_files(tmp_path):
    """FX-1 must invoke ruff with specific changed files, not the entire src/ dir."""
    ruff_invocations = []

    def fake_check_output(cmd, **kwargs):
        # Simulate git log returning 2 changed .py files
        if "git" in cmd[0] and "log" in cmd:
            return "src/juggle_db.py\nsrc/juggle_cli.py\n"
        return ""

    def fake_subprocess_run(cmd, **kwargs):
        ruff_invocations.append(list(cmd))
        return MagicMock(returncode=0, stdout="", stderr="")

    # Make the changed files actually exist
    (tmp_path / "src").mkdir()
    db_file = tmp_path / "src" / "juggle_db.py"
    cli_file = tmp_path / "src" / "juggle_cli.py"
    db_file.write_text("# db")
    cli_file.write_text("# cli")

    pr_sections = {}
    with (
        patch(
            "juggle_schedule_autofix.subprocess.check_output",
            side_effect=fake_check_output,
        ),
        patch(
            "juggle_schedule_autofix.subprocess.run", side_effect=fake_subprocess_run
        ),
        patch.object(autofix, "JUGGLE_REPO", tmp_path),
        patch.object(autofix, "_git_diff_stat", return_value={}),
    ):
        autofix.fx1_ruff("test-branch", dry_run=True, pr_sections=pr_sections)

    # Find the ruff --fix invocation
    ruff_calls = [c for c in ruff_invocations if "check" in c and "--fix" in c]
    assert ruff_calls, "ruff --fix was never called"
    ruff_cmd = ruff_calls[0]

    # Must NOT contain the full src/ directory
    assert not any(arg.endswith("/src") or arg == "src" for arg in ruff_cmd), (
        f"FX-1 ran ruff on entire src/ dir: {ruff_cmd}"
    )

    # Must contain specific file paths (not src/ glob)
    file_args = [a for a in ruff_cmd if a.endswith(".py")]
    assert len(file_args) >= 1, f"No .py files in ruff invocation: {ruff_cmd}"
