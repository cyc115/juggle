"""Tests for juggle_schedule_dogfood."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import juggle_schedule_common as common
import juggle_schedule_dogfood as dogfood


# ---------------------------------------------------------------------------
# Idempotency: prior open dogfood thread blocks run
# ---------------------------------------------------------------------------


def test_prior_dogfood_thread_blocks_run(tmp_path):
    mock_db = MagicMock()
    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(
            dogfood, "_check_prior_dogfood_thread", return_value="dogfood-2026-05-11"
        ),
        patch.object(dogfood, "_check_active_session", return_value=False),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = dogfood.run(dry_run=True)
    assert result == 1


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------


def test_dry_run_writes_report(tmp_path):
    mock_db = MagicMock()
    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None),
        patch.object(dogfood, "_check_active_session", return_value=False),
        patch.object(dogfood, "_tmux_session_exists", return_value=False),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = dogfood.run(dry_run=True)

    assert result == 0
    report = Path("/tmp/schedule-dogfood-sample-report.md")
    assert report.exists()
    content = report.read_text()
    assert "Juggle Self-Analysis" in content or "DRY RUN" in content


# ---------------------------------------------------------------------------
# Idempotency: dry_run does not file action items
# ---------------------------------------------------------------------------


def test_dry_run_does_not_file_action_items(tmp_path):
    mock_db = MagicMock()
    add_calls = []
    mock_db.add_action_item = lambda **kw: add_calls.append(kw)

    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None),
        patch.object(dogfood, "_check_active_session", return_value=False),
        patch.object(dogfood, "_tmux_session_exists", return_value=False),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        dogfood.run(dry_run=True)

    assert len(add_calls) == 0


# ---------------------------------------------------------------------------
# Active session check
# ---------------------------------------------------------------------------


def test_active_session_defers_and_aborts(tmp_path):
    mock_db = MagicMock()
    add_calls = []
    mock_db.add_action_item = lambda **kw: add_calls.append(kw)

    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None),
        patch.object(dogfood, "_check_active_session", return_value=True),
        patch.object(
            dogfood, "_find_or_create_schedule_thread", return_value="thread-1"
        ),
        patch("time.sleep"),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = dogfood.run(dry_run=False)

    assert result == 1


# ---------------------------------------------------------------------------
# Cost cap
# ---------------------------------------------------------------------------


def test_cost_cap_writes_partial_report(tmp_path):
    mock_db = MagicMock()

    def raise_cost_cap(*args, **kwargs):
        raise common.CostCapExceeded("dogfood: cap exceeded")

    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None),
        patch.object(dogfood, "_check_active_session", return_value=False),
        patch.object(dogfood, "_tmux_session_exists", return_value=False),
        patch.object(dogfood, "_run_headless_research", side_effect=raise_cost_cap),
        patch.object(dogfood, "_file_action_item"),
        patch.object(dogfood, "_write_and_commit"),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = dogfood.run(dry_run=True)

    assert result == 1


# ---------------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------------


def test_build_report_contains_required_sections():
    agent_output = (
        "## Observed Friction Patterns\n1. Stalls\n\n"
        "## Repeated Dispatches / Blockers\nNone\n\n"
        "## Suggested Improvements (1–3)\n1. Add timeout\n"
    )
    report = dogfood._build_report("2026-05-11", agent_output, 0.25)
    assert "Juggle Self-Analysis" in report
    assert "2026-05-11" in report
    assert "Observed Friction Patterns" in report
    assert "$0.25" in report


# ---------------------------------------------------------------------------
# File action item — extracts first suggestion
# ---------------------------------------------------------------------------


def test_file_action_item_extracts_suggestion():
    mock_db = MagicMock()
    filed = []
    mock_db.add_action_item = lambda **kw: filed.append(kw)

    findings = "## Suggested Improvements (1–3)\n1. Reduce confirmation prompts in hooks.py:42\n"
    with patch.object(
        dogfood, "_find_or_create_schedule_thread", return_value="thread-1"
    ):
        dogfood._file_action_item(mock_db, findings, dry_run=False)

    assert len(filed) == 1
    assert "Dogfood findings" in filed[0]["message"]
    assert filed[0]["type_"] == "decision"
    assert filed[0]["priority"] == "high"


def test_file_action_item_no_findings_message():
    mock_db = MagicMock()
    filed = []
    mock_db.add_action_item = lambda **kw: filed.append(kw)

    with patch.object(
        dogfood, "_find_or_create_schedule_thread", return_value="thread-1"
    ):
        dogfood._file_action_item(mock_db, "no suggestions here", dry_run=False)

    assert len(filed) == 1
    assert "NO FINDINGS" in filed[0]["message"]


# ---------------------------------------------------------------------------
# Bug 3 regression: dry-run spawn path exercises create-thread/get-agent/send-task
# ---------------------------------------------------------------------------


def test_canary_spawn_calls_cli_commands():
    """_run_canary_spawn must invoke create-thread, get-agent, and send-task via CLI."""
    subprocess_calls = []

    def fake_run(cmd, **kwargs):
        subprocess_calls.append(list(cmd))
        mock_result = MagicMock()
        mock_result.returncode = 0
        if "get-agent" in cmd:
            mock_result.stdout = "agent-abc12345 %5\n"
        elif "check-agents" in cmd:
            mock_result.stdout = json.dumps(
                [{"id": "agent-abc12345", "status": "idle"}]
            )
        elif "create-thread" in cmd:
            mock_result.stdout = (
                "Created Topic JA: dogfood-dryrun-2026-05-19. Now in Topic JA.\n"
            )
        else:
            mock_result.stdout = ""
        mock_result.stderr = ""
        return mock_result

    ct = common.CostTracker(cap_usd=1.0, routine="dogfood", dry_run=True)
    with (
        patch("juggle_schedule_dogfood.subprocess.run", side_effect=fake_run),
        patch("juggle_schedule_dogfood.time.sleep"),
    ):
        dogfood._run_canary_spawn(ct)

    cmd_strings = [" ".join(c) for c in subprocess_calls]
    assert any("create-thread" in c for c in cmd_strings), (
        f"create-thread not called. Calls: {cmd_strings}"
    )
    assert any("get-agent" in c for c in cmd_strings), (
        f"get-agent not called. Calls: {cmd_strings}"
    )
    assert any("send-task" in c for c in cmd_strings), (
        f"send-task not called. Calls: {cmd_strings}"
    )


def test_dry_run_uses_canary_when_tmux_available(tmp_path):
    """When tmux session exists, dry-run calls _run_canary_spawn instead of returning fake output."""
    mock_db = MagicMock()
    canary_called = []

    def fake_canary(ct):
        canary_called.append(True)
        return "## Observed Friction Patterns\n[DRY-RUN-CANARY] Spawn path exercised.\n"

    with (
        patch.object(dogfood, "get_db", return_value=mock_db),
        patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None),
        patch.object(dogfood, "_check_active_session", return_value=False),
        patch.object(dogfood, "_tmux_session_exists", return_value=True),
        patch.object(dogfood, "_run_canary_spawn", side_effect=fake_canary),
        patch.object(dogfood, "REPORTS_DIR", tmp_path),
        patch.object(common, "STATE_FILE", tmp_path / "state.json"),
        patch.object(common, "JUGGLE_DIR", tmp_path),
    ):
        result = dogfood.run(dry_run=True)

    assert result == 0
    assert len(canary_called) == 1, "canary spawn was not called"
    report = Path("/tmp/schedule-dogfood-sample-report.md")
    assert "DRY-RUN-CANARY" in report.read_text()
