"""CLI tests: open-in-editor + _parse_path_with_line (split from test_juggle_cli.py, 2026-06-10)."""

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
# open-in-editor tests
# ---------------------------------------------------------------------------


def test_open_in_editor_no_socket(tmp_path):
    """Falls back to system open (non-vault file) when socket doesn't exist."""
    from juggle_cli import main

    socket_path = str(tmp_path / "missing.sock")
    with patch("sys.argv", ["juggle_cli.py", "open-in-editor", "/some/file.md"]):
        with patch("juggle_cli.NVIM_SOCKET", socket_path):
            with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
                with patch("juggle_cli.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    main()
    mock_run.assert_called_once_with(["open", "/some/file.md"], check=True)


def test_open_in_editor_no_socket_vault_file(tmp_path):
    """Falls back to Obsidian URL when socket is absent and file is inside vault."""
    from juggle_cli import main, VAULT_ROOT

    socket_path = str(tmp_path / "missing.sock")
    vault_file = str(VAULT_ROOT / "projects/test.md")
    with patch("sys.argv", ["juggle_cli.py", "open-in-editor", vault_file]):
        with patch("juggle_cli.NVIM_SOCKET", socket_path):
            with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
                with patch("juggle_cli.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    main()
    url = mock_run.call_args[0][0][1]
    assert url.startswith("obsidian://open?vault=personal&file=projects/test.md")


def test_open_in_editor_calls_nvim(tmp_path):
    """Calls nvim --server --remote when socket exists (no line number)."""
    from juggle_cli import main

    sock = tmp_path / "nvim.sock"
    sock.touch()
    with patch("sys.argv", ["juggle_cli.py", "open-in-editor", "/some/file.md"]):
        with patch("juggle_cli.NVIM_SOCKET", str(sock)):
            with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
                with patch("juggle_cli.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    main()
    mock_run.assert_called_once_with(
        ["nvim", "--server", str(sock), "--remote", "/some/file.md"],
        check=True,
    )


def test_open_in_editor_with_line_number(tmp_path):
    """Sends remote-send to position cursor when path:line syntax is used."""
    from juggle_cli import main

    sock = tmp_path / "nvim.sock"
    sock.touch()
    with patch("sys.argv", ["juggle_cli.py", "open-in-editor", "/some/file.py:153"]):
        with patch("juggle_cli.NVIM_SOCKET", str(sock)):
            with patch.dict(os.environ, {"_JUGGLE_TEST_DB": "1"}):
                with patch("juggle_cli.subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    main()
    calls = mock_run.call_args_list
    assert len(calls) == 2
    assert calls[0][0][0] == [
        "nvim",
        "--server",
        str(sock),
        "--remote",
        "/some/file.py",
    ]
    assert calls[1][0][0] == [
        "nvim",
        "--server",
        str(sock),
        "--remote-send",
        "<C-\\><C-N>:153<CR>",
    ]


# ---------------------------------------------------------------------------
# _parse_path_with_line unit tests
# ---------------------------------------------------------------------------


def test_parse_path_with_line_no_suffix():
    from juggle_cli import _parse_path_with_line

    assert _parse_path_with_line("foo.py") == ("foo.py", None)


def test_parse_path_with_line_basic():
    from juggle_cli import _parse_path_with_line

    assert _parse_path_with_line("foo.py:42") == ("foo.py", 42)


def test_parse_path_with_line_col():
    from juggle_cli import _parse_path_with_line

    assert _parse_path_with_line("foo.py:42:5") == ("foo.py", 42)


def test_parse_path_with_line_abs():
    from juggle_cli import _parse_path_with_line

    assert _parse_path_with_line("/abs/path.py:1") == ("/abs/path.py", 1)


def test_parse_path_with_line_no_trailing_digits():
    from juggle_cli import _parse_path_with_line

    assert _parse_path_with_line("weird:name.py") == ("weird:name.py", None)
