"""CLI tests: `juggle verify` — the agent-facing one-shot verification helper.

Regression context (2026-06-20): coder agents zombie-looped re-running the suite
as a background job and polling it forever. `juggle verify` exists so agents call
ONE deterministic command that runs the FULL suite ONCE, synchronously — no
hand-rolled pytest, no background-poll loop, no subset/--deselect (the full-suite
directive). These tests pin that contract.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_DIR_TOP = str(Path(__file__).parent.parent / "src")
if SRC_DIR_TOP not in sys.path:
    sys.path.insert(0, SRC_DIR_TOP)


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


def test_verify_runs_full_suite_no_deselect():
    """Full-suite directive (2026-06-20): `juggle verify` runs the WHOLE suite —
    the exec'd pytest command carries NO --deselect (no subset, no quarantine).
    Pre-fix it read integrate.quarantine_tests and prepended --deselect flags."""
    mock_run, _ = _run_verify()
    cmd = mock_run.call_args.args[0]
    flat = cmd if isinstance(cmd, str) else " ".join(cmd)
    assert "--deselect" not in flat, (
        f"verify must run the FULL suite with no deselect: {flat}"
    )
    # It IS pytest, run as the bare full-suite command.
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


def test_verify_passthrough_arg_with_spaces_not_resplit():
    """An arg containing spaces (e.g. -k "a or b") must reach pytest as a single
    token, not be re-split. Guards the cmd_verify arg-handling against a lossy
    string round-trip (passthrough args are kept as discrete list items)."""
    mock_run, _ = _run_verify(["-k", "a or b"])
    cmd = mock_run.call_args.args[0]
    assert isinstance(cmd, list), "verify must exec a token list, not a shell string"
    assert "a or b" in cmd, f"-k expression was re-split: {cmd}"


def test_verify_leading_flag_passthrough():
    """`juggle verify -k foo` (flag first, no preceding path) must reach pytest —
    guards against the argparse.REMAINDER leading-flag bug that the
    parse_known_args routing replaced."""
    mock_run, _ = _run_verify(["-k", "foo"])
    cmd = mock_run.call_args.args[0]
    assert "-k" in cmd and "foo" in cmd, f"leading-flag passthrough lost: {cmd}"


def test_unknown_flag_on_other_command_still_errors():
    """The parse_known_args leniency is scoped to `verify` ONLY — every other
    command must still hard-reject an unknown flag (typo protection)."""
    with patch("sys.argv", ["juggle_cli.py", "vault-path", "--nonsense-typo"]):
        with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
            with pytest.raises(SystemExit) as exc:
                from juggle_cli import main
                main()
    assert exc.value.code == 2, "unknown flag on a non-verify command must exit 2"
