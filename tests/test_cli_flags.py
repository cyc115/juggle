"""CLI argparse regression pins: unknown-flag rejection (fixes TODO.md:38) (split from test_juggle_cli.py, 2026-06-10)."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_DIR_TOP = str(Path(__file__).parent.parent / "src")
if SRC_DIR_TOP not in sys.path:
    sys.path.insert(0, SRC_DIR_TOP)
from juggle_cli import _last_sentences

CLI = str(Path(__file__).parent.parent / "src" / "juggle_cli.py")
SRC_DIR = str(Path(__file__).parent.parent / "src")


def run_cli(args, db_path):
    """Run juggle_cli.py with a test DB path, override DB_PATH via env."""
    import os

    env = os.environ.copy()
    # Patch by passing db_path via a temporary monkeypatch in subprocess
    result = subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        env={**env, "_JUGGLE_TEST_DB": str(db_path)},
    )
    return result


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


@pytest.fixture
def started_db(tmp_path):
    """Return (db_path, general_thread_uuid) after running `start`."""
    db_path = tmp_path / "test.db"
    sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("General", session_id="")
    db.set_current_thread(tid)
    return db_path, tid




# ---------------------------------------------------------------------------
# Unknown flag rejection — regression tests (fixes TODO.md:38)
# ---------------------------------------------------------------------------


def _run_no_db(args):
    """Run CLI without _JUGGLE_TEST_DB so argparse errors fire before DB init."""
    env = os.environ.copy()
    env.pop("_JUGGLE_TEST_DB", None)
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True,
        text=True,
        env=env,
    )


def test_unknown_flag_rejected_request_action(tmp_path):
    """--tier is not a valid flag on request-action; argparse must reject it."""
    result = _run_no_db(["request-action", "THREAD", "msg", "--tier", "2"])
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr or "error" in result.stderr


def test_unknown_flag_rejected_complete_agent(tmp_path):
    """--badarg is not a valid flag on complete-agent; argparse must reject it."""
    result = _run_no_db(["complete-agent", "AGENT", "msg", "--badarg"])
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr or "error" in result.stderr


def test_unknown_flag_rejected_start(tmp_path):
    """--nosuchflag is not a valid flag on start; argparse must reject it."""
    result = _run_no_db(["start", "--nosuchflag"])
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr or "error" in result.stderr


def test_unknown_flag_rejected_notify(tmp_path):
    """--priority is not a valid flag on notify; argparse must reject it."""
    result = _run_no_db(["notify", "THREAD", "msg", "--priority", "high"])
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr or "error" in result.stderr


def test_known_flags_accepted_request_action(tmp_path):
    """Valid flags on request-action (--type, --priority) must not be rejected at parse time."""
    db_path = tmp_path / "test.db"
    import sys as _sys

    _sys.path.insert(0, SRC_DIR)
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("test", session_id="")
    result = run_cli(
        ["request-action", tid, "test msg", "--type", "failure", "--priority", "high"],
        db_path,
    )
    # Should not fail with "unrecognized arguments"
    assert "unrecognized arguments" not in result.stderr

