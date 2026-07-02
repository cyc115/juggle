"""Spool test isolation (2026-07-02 incident): a test/drain run must never
touch the real ~/.juggle/spool.

INCIDENT 2026-07-02 ~03:25-04:20 local: 79 production spool events were
consumed and dead-lettered (~/.juggle/spool/dead/, prod spool_journal shows 0
applies) by agent worktree TEST RUNS of tests/test_spool_drain_idempotent.py
and related suites. Root cause: drain_spool / spool_dir() resolved the REAL
~/.juggle/spool while tests journaled into per-test tmp DBs — so live agent
agent_complete / graph_mark_task events (AZ/BB/BF/BI/BH/BJ) were silently
destroyed; the orchestrator had to hand-repair each. Example dead event:
dead_reason "SystemExit during replay of graph_mark_task: code=1".

Fixes pinned here:
  1. `spool_dir()` honors a `JUGGLE_SPOOL_DIR` env override.
  2. The conftest autouse fixture points every test's spool at a per-test tmp
     dir AND hard-fails (raises) the instant any test targets the real spool
     dir directly (bypassing spool_dir()).
"""
from __future__ import annotations

import json
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import dbops.spool as spool  # noqa: E402
import juggle_spool_paths  # noqa: E402
from _spool_isolation import real_spool_dir  # noqa: E402

# NOTE: guarded functions are called via the `spool.` module namespace
# throughout this file, never via `from dbops.spool import write_event` — a
# direct name import freezes the pre-patch binding, invisible to the
# conftest autouse guard (same reason juggle_spool_apply.py needs its own
# monkeypatch target — see conftest.py's `_isolate_spool_from_prod`).


# ---------------------------------------------------------------------------
# 1. JUGGLE_SPOOL_DIR override
# ---------------------------------------------------------------------------


def test_spool_dir_honors_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom-spool"
    monkeypatch.setenv("JUGGLE_SPOOL_DIR", str(target))
    d = juggle_spool_paths.spool_dir()
    assert d == target
    assert d.is_dir()


# ---------------------------------------------------------------------------
# 2. Fail-closed prod-spool guard (installed by the autouse conftest fixture)
# ---------------------------------------------------------------------------


def test_reading_real_spool_dir_raises():
    """Any attempt to read the real spool dir during a test is a loud error
    (mirrors test_db_isolation.test_opening_prod_db_raises)."""
    with pytest.raises(RuntimeError, match="(?i)isolation"):
        spool.read_pending(real_spool_dir())


def test_writing_real_spool_dir_raises():
    with pytest.raises(RuntimeError, match="(?i)isolation"):
        spool.write_event(real_spool_dir(), "agent_complete", "a", "t", {})


def test_move_to_dead_on_real_spool_dir_raises(tmp_path):
    fake_file = tmp_path / "irrelevant.json"
    fake_file.write_text("{}")
    with pytest.raises(RuntimeError, match="(?i)isolation"):
        spool.move_to_dead(real_spool_dir(), fake_file, "boom")


def test_temp_spool_read_is_allowed(tmp_path):
    """Non-prod spool dirs work normally — the guard only fires on the real one."""
    d = tmp_path / "spool"
    d.mkdir()
    spool.write_event(d, "agent_complete", "a", "t", {})
    assert len(spool.read_pending(d)) == 1


# ---------------------------------------------------------------------------
# 3. Regression pin: sentinel event in a FAKE "real" spool dir must survive a
#    drain attempt (2026-07-02 incident reproduction, without ever touching
#    the genuine ~/.juggle/spool — HOME is repointed at a tmp dir, so
#    real_spool_dir() resolves under tmp_path for the duration of this test).
# ---------------------------------------------------------------------------


def test_sentinel_in_fake_real_spool_dir_survives_drain_attempt(monkeypatch, tmp_path):
    fake_home = tmp_path / "fake-home"
    (fake_home / ".juggle" / "spool").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("JUGGLE_SPOOL_DIR", raising=False)

    prod = real_spool_dir()
    assert prod == (fake_home / ".juggle" / "spool").resolve()

    sentinel = prod / "0001-real-agent-complete.json"
    sentinel.write_text(json.dumps({
        "uuid": "sentinel-uuid", "type": "agent_complete", "agent_id": "real-agent",
        "thread_id": "real-thread", "args": {}, "created_at": "2026-07-02T03:25:00Z",
    }))

    # A test/drain run reading this "real" dir must refuse — not consume it.
    with pytest.raises(RuntimeError, match="(?i)isolation"):
        spool.read_pending(prod)

    assert sentinel.exists()
    assert json.loads(sentinel.read_text())["uuid"] == "sentinel-uuid"
