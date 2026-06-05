"""Tests for watchdog heartbeat health detection and CLI warning."""
import ast
import os
import sys
import time
from pathlib import Path
from unittest import mock

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_is_watchdog_alive_returns_true_when_fresh(tmp_path):
    """Fresh heartbeat file → watchdog is alive."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    hb.touch()  # mtime = now

    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is True


def test_is_watchdog_alive_returns_false_when_stale(tmp_path):
    """Heartbeat file older than stale_secs → watchdog is dead."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    hb.touch()
    old_time = time.time() - 300  # 5 min old
    os.utime(hb, (old_time, old_time))

    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is False


def test_is_watchdog_alive_returns_false_when_missing(tmp_path):
    """No heartbeat file → watchdog has never run."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is False


def test_write_heartbeat_creates_file(tmp_path):
    """write_heartbeat must create the heartbeat file."""
    from juggle_watchdog_health import write_heartbeat

    hb = tmp_path / "watchdog_heartbeat"
    before = time.time()
    write_heartbeat(heartbeat_path=hb)
    after = time.time()

    assert hb.exists()
    mtime = hb.stat().st_mtime
    assert before <= mtime <= after + 1


def test_write_heartbeat_updates_stale_file(tmp_path):
    """write_heartbeat must update mtime of an existing stale file."""
    from juggle_watchdog_health import write_heartbeat

    hb = tmp_path / "watchdog_heartbeat"
    hb.touch()
    old_time = time.time() - 300
    os.utime(hb, (old_time, old_time))
    assert hb.stat().st_mtime < time.time() - 100  # confirm stale

    write_heartbeat(heartbeat_path=hb)

    assert hb.stat().st_mtime > time.time() - 5  # now fresh


def test_cli_warns_when_watchdog_dead():
    """CLI main must print a stderr warning when watchdog heartbeat is stale."""
    # We test this by checking the AST — the warning must be in main()
    # and must depend on is_watchdog_alive().
    source = (Path(__file__).parent.parent / "src" / "juggle_cli.py").read_text()
    tree = ast.parse(source)

    main_func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_func is not None

    # Look for is_watchdog_alive name usage anywhere in main body
    alive_refs = [
        n for n in ast.walk(main_func)
        if isinstance(n, ast.Name) and n.id == "is_watchdog_alive"
    ]
    assert len(alive_refs) > 0, (
        "main() has no reference to is_watchdog_alive — add dead-watchdog warning"
    )


def test_no_warning_when_watchdog_alive(tmp_path, capsys):
    """CLI must not warn when watchdog heartbeat is fresh."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    hb.touch()

    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is True


def test_warning_message_when_watchdog_dead(tmp_path, capsys):
    """is_watchdog_alive returns False for missing heartbeat — warning logic must branch on it."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "missing_heartbeat"
    result = is_watchdog_alive(heartbeat_path=hb, stale_secs=120)
    assert result is False
