"""Regression pins — smoke-seeded watchdog must be HERMETIC (2026-07-04 incident).

DEFECT: `juggle cockpit --smoke [--smoke-plan]` spawns a child cockpit against a
TEMP seeded DB. That child's on_mount ensure-exists a DETACHED watchdog which,
seeded with the graph fixture, behaved as a REAL orchestrator:
  1. dispatched the fixture's placeholder nodes as REAL tmux coder agents,
     creating REAL worktrees/branches off the PROD repo (cyc_AA..cyc_AE); and
  2. wrote agent_complete/graph_mark_task events to the GLOBAL spool
     (~/.juggle/spool is not per-DB), which the PROD watchdog then drained and
     dead-lettered ("not found" — thread absent from prod DB) → the day's
     "SystemExit code=1" dead-letter storm.

The operator-facing --smoke path set NEITHER hermetic seam that pytest's conftest
sets for its own runs (JUGGLE_WATCHDOG_DISABLE_SPAWN=1 + a per-run JUGGLE_SPOOL_DIR),
so the child ran unsandboxed. These pins assert the harness now builds that
hermetic env and threads it into the child cockpit spawner.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("yaml", reason="pyyaml not installed")


# ── pin: the hermetic env overlay closes both seams ───────────────────────────


def test_hermetic_smoke_env_disables_watchdog_and_isolates_spool(tmp_path):
    """The overlay MUST disable the real detached watchdog (no dispatch → no
    worktree/branch in the real repo) AND point the spool at a per-run dir (so
    a spool event can never reach the prod drain)."""
    from juggle_smoke import hermetic_smoke_env

    env = hermetic_smoke_env(tmp_path)

    assert env["JUGGLE_WATCHDOG_DISABLE_SPAWN"] == "1", (
        "smoke child must never launch a real orchestrating daemon"
    )
    spool = Path(env["JUGGLE_SPOOL_DIR"])
    assert tmp_path in spool.parents or spool == tmp_path / "spool", (
        f"spool dir must live under the run dir, got {spool}"
    )
    assert spool.exists(), "spool dir should be created so writers land there"


def test_hermetic_env_disable_gate_stops_real_watchdog_spawn(tmp_path, monkeypatch):
    """Behavioral: under the overlay, ensure_watchdog(spawn=None) must NOT launch
    a detached daemon — the disable gate short-circuits before start_watchdog_detached.
    This is the seam that prevented the cyc_AA..AE real worktrees/branches."""
    from juggle_smoke import hermetic_smoke_env
    import juggle_watchdog_singleton as wds

    for k, v in hermetic_smoke_env(tmp_path).items():
        monkeypatch.setenv(k, v)

    launched = {"n": 0}

    def _spy_start(db_path, *, repo_path=None):
        launched["n"] += 1
        return 12345

    monkeypatch.setattr(wds, "start_watchdog_detached", _spy_start)
    monkeypatch.setattr(wds, "is_watchdog_alive", lambda db: False)

    db_path = str(tmp_path / "juggle.db")
    launched_new = wds.ensure_watchdog(db_path)  # spawn=None → real path, gated

    assert launched_new is False, "ensure_watchdog must not report a launch"
    assert launched["n"] == 0, "no detached daemon may be spawned under the smoke overlay"


def test_hermetic_env_spool_write_lands_in_run_dir_not_prod(tmp_path, monkeypatch):
    """Pin (a): a spool event written under the overlay lands in the run's own
    spool dir; the prod spool path is never touched."""
    from juggle_smoke import hermetic_smoke_env
    from juggle_spool_paths import spool_dir
    from dbops.spool import write_event, read_pending

    env = hermetic_smoke_env(tmp_path)
    monkeypatch.setenv("JUGGLE_SPOOL_DIR", env["JUGGLE_SPOOL_DIR"])

    run_spool = spool_dir()
    assert run_spool == Path(env["JUGGLE_SPOOL_DIR"]), "spool_dir must honor the overlay"

    write_event(run_spool, "agent_complete", "AA", "thread-x", {"note": "smoke"})
    pending = read_pending(run_spool)
    assert len(pending) == 1, "the smoke event must land in the run spool dir"
    # Prod default path (~/.juggle/spool) must not exist / be untouched here.
    assert env["JUGGLE_SPOOL_DIR"] != str(Path("~/.juggle/spool").expanduser())


# ── pin: the runner threads the overlay into the child spawner ────────────────


def test_cmd_cockpit_smoke_threads_hermetic_env_to_child(monkeypatch, tmp_path):
    """Top-level wiring: `cockpit --smoke` must build the hermetic overlay and
    pass it to run_smoke (→ open_cockpit_pty child env). Before the fix the
    operator path passed no env, so the child spawned a real daemon."""
    import juggle_smoke

    captured = {}

    def _fake_run_smoke(viewports, db_path=None, output_dir=None, interactive=False,
                        graph_mode=False, plan_mode=False, env=None):
        captured["env"] = env
        captured["db_path"] = db_path
        return [{"profile": "p", "cols": 80, "rows": 40, "pass": True}]

    monkeypatch.setattr(juggle_smoke, "run_smoke", _fake_run_smoke)

    from juggle_cmd_misc import cmd_cockpit

    args = types.SimpleNamespace(
        smoke=True, viewport_name=None, interactive=False, db_path=None,
        smoke_plan=False, smoke_graph=False, json_out=True,
    )
    with pytest.raises(SystemExit):
        cmd_cockpit(args)

    env = captured.get("env")
    assert env is not None, "cmd_cockpit --smoke must pass an env overlay to run_smoke"
    assert env["JUGGLE_WATCHDOG_DISABLE_SPAWN"] == "1"
    assert "JUGGLE_SPOOL_DIR" in env


def test_run_smoke_forwards_env_to_open_cockpit_pty(monkeypatch, tmp_path):
    """run_smoke must forward its env overlay to open_cockpit_pty so the child
    cockpit inherits the hermetic seams."""
    import juggle_smoke

    seen = {}

    class _FakeHandle:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def frame(self, settle=1.0, timeout=10.0):
            grid = [" " * 80 for _ in range(40)]
            grid[0] = "Juggle Cockpit".ljust(80)
            grid[-1] = "q Quit".ljust(80)
            for i in range(1, 39):
                grid[i] = f"row{i:03d} body".ljust(80)
            return grid

        def send(self, *a):
            pass

    def _fake_open(profile, db_path=None, env=None):
        seen["env"] = env
        return _FakeHandle()

    monkeypatch.setattr(juggle_smoke, "open_cockpit_pty", _fake_open)

    overlay = {"JUGGLE_WATCHDOG_DISABLE_SPAWN": "1", "JUGGLE_SPOOL_DIR": str(tmp_path / "s")}
    juggle_smoke.run_smoke(
        {"p": {"cols": 80, "rows": 40}},
        db_path=str(tmp_path / "db.sqlite"),
        output_dir=tmp_path / "out",
        env=overlay,
    )

    assert seen.get("env") == overlay, "run_smoke must forward env to open_cockpit_pty"
