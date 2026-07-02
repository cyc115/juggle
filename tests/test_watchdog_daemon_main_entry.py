"""Regression pins: the watchdog daemon module must run as a real entrypoint.

Incident: 2026-06-17 watchdog-start-fix.
Symptom: cockpit `r` reported "watchdog restarted from main" but the status dot
never went green — the singleton spawner launches the daemon MODULE FILE
directly (`uv run python src/juggle_watchdog_daemon.py`), but the module had no
`if __name__ == "__main__": main()` guard, so it imported, defined main(), and
exited 0 instantly without ever ticking or acquiring the singleton lock.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DAEMON_SRC = REPO_ROOT / "src" / "juggle_watchdog_daemon.py"


def test_daemon_module_has_main_guard():
    """Cheap headless pin: the module must call main() under a __main__ guard."""
    text = DAEMON_SRC.read_text()
    assert 'if __name__ == "__main__":' in text, (
        "daemon module missing `if __name__ == \"__main__\":` guard — direct "
        "module launch (how the singleton spawner runs it) would never call main()"
    )
    guard_idx = text.index('if __name__ == "__main__":')
    assert "main()" in text[guard_idx:], (
        "daemon __main__ guard must call main()"
    )


def test_sanctioned_direct_launch_stays_alive(tmp_path):
    """Integration pin: spawning the daemon EXACTLY as the singleton spawner does
    (`uv run python src/juggle_watchdog_daemon.py`, cwd=repo root) against an
    isolated temp DB must stay alive and acquire the per-DB singleton lock.

    Pre-fix this RED-fails: the module exits 0 instantly (no __main__ guard), so
    is_watchdog_alive never becomes True.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    import juggle_watchdog_singleton as wsg

    db_path = tmp_path / "juggle.db"
    log_path = tmp_path / "daemon.log"

    env = os.environ.copy()
    env["JUGGLE_DB_PATH"] = str(db_path)
    env["JUGGLE_ORCHESTRATOR"] = "1"
    env[wsg.SANCTION_ENV] = "1"
    # Isolate the daemon's log + snapshot/recovery dirs to tmp so a test-spawned
    # daemon NEVER writes the shared ~/.juggle/watchdog.log (2026-07-01: test
    # daemons polluted the prod watchdog log and looked like prod thrashing).
    env["JUGGLE_CONFIG_DIR"] = str(tmp_path)
    env.pop("JUGGLE_IS_AGENT", None)
    env.pop("JUGGLE_WATCHDOG_SUPERVISED", None)

    log_fd = open(log_path, "wb")
    proc = subprocess.Popen(
        ["uv", "run", "python", "src/juggle_watchdog_daemon.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    try:
        # 30s (was 15s): `uv run` cold-start + daemon init can lag under a
        # CPU-saturated `-n auto` run (speedup-tier, 2026-06-21). Still asserts
        # the daemon becomes alive and holds the lock — just more patient.
        deadline = time.monotonic() + 30.0
        alive = False
        while time.monotonic() < deadline:
            if wsg.is_watchdog_alive(db_path):
                alive = True
                break
            if proc.poll() is not None:
                log_fd.flush()
                log = log_path.read_text() if log_path.exists() else "<no log>"
                raise AssertionError(
                    f"daemon exited early (rc={proc.returncode}) before becoming "
                    f"alive — log:\n{log}"
                )
            time.sleep(0.2)
        assert alive, (
            "daemon never acquired the singleton lock within 15s — log:\n"
            + (log_path.read_text() if log_path.exists() else "<no log>")
        )
    finally:
        # Reap the whole process GROUP, not just the `uv run` parent: the daemon
        # is spawned with start_new_session=True, so `uv run` leads its own
        # session/process-group and the real python daemon CHILD is detached.
        # Terminating only `proc` would orphan that child, which keeps holding
        # the singleton lock and ticking (2026-06-21 daemon-teardown leak; the
        # autouse survivor guard now fails any test that leaks one). killpg
        # SIGTERM→SIGKILL reaps parent + child together.
        try:
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = None
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if pgid is None:
                    break
                try:
                    os.killpg(pgid, sig)
                except ProcessLookupError:
                    break
                try:
                    proc.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            log_fd.close()
