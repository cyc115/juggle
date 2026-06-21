"""CLI tests: `juggle verify` — the agent-facing one-shot verification helper.

Regression context (2026-06-20): coder agents zombie-looped re-running the FULL
suite on pre-existing quarantined reds (loc_gate/data_migration/test_integrate).
`juggle verify` exists so agents call ONE deterministic command that auto-applies
the quarantine --deselect flags and runs the suite ONCE, synchronously — no
hand-rolled pytest, no background-poll loop. These tests pin that contract.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_DIR_TOP = str(Path(__file__).parent.parent / "src")
if SRC_DIR_TOP not in sys.path:
    sys.path.insert(0, SRC_DIR_TOP)


def _pair(cmd, qt):
    """Return the [--deselect, qt] slice if present in a list-form cmd, else []."""
    if isinstance(cmd, str):
        return []
    for i, tok in enumerate(cmd[:-1]):
        if tok == "--deselect" and cmd[i + 1] == qt:
            return ["--deselect", qt]
    return []


def _run_verify(extra_argv=None):
    """Invoke `juggle verify` via main() with subprocess.run mocked.

    Returns (mock_run, exit_code). The mock captures the pytest invocation.
    """
    argv = ["juggle_cli.py", "verify"] + (extra_argv or [])
    with patch("sys.argv", argv):
        with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
            with patch("juggle_cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                with pytest.raises(SystemExit) as exc:
                    from juggle_cli import main
                    main()
    return mock_run, exc.value.code


def test_verify_runs_pytest_once_synchronously():
    """One foreground subprocess.run — never backgrounded, never looped."""
    mock_run, code = _run_verify()
    mock_run.assert_called_once()
    # Synchronous: not a Popen, and no shell.
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("shell", False) is False, "verify must not use a shell"
    assert code == 0


def test_verify_auto_applies_quarantine_deselects():
    """The exec'd pytest command deselects every integrate.quarantine_tests path."""
    from juggle_settings import get_settings

    mock_run, _ = _run_verify()
    cmd = mock_run.call_args.args[0]
    flat = cmd if isinstance(cmd, str) else " ".join(cmd)
    quarantine = get_settings()["integrate"]["quarantine_tests"]
    assert quarantine, "precondition: quarantine list is non-empty in defaults"
    for qt in quarantine:
        assert f"--deselect {qt}" in flat or _pair(cmd, qt) == ["--deselect", qt], (
            f"verify did not deselect quarantined test {qt}: {flat}"
        )
    # It IS pytest.
    assert "pytest" in flat


def test_verify_exit_code_propagates_failure():
    """A red suite (non-zero pytest) makes `juggle verify` exit non-zero — so an
    agent's `juggle verify && complete-agent` chain fails loudly instead of
    silently 'passing'."""
    argv = ["juggle_cli.py", "verify"]
    with patch("sys.argv", argv):
        with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
            with patch("juggle_cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                with pytest.raises(SystemExit) as exc:
                    from juggle_cli import main
                    main()
    assert exc.value.code == 1
