"""Global test isolation (2026-06-16 prod-DB pollution incident).

Two fail-closed guards applied to EVERY test:

  1. `JUGGLE_DB_PATH` is pointed at a per-test temp DB, so any bare `JuggleDB()`
     (or product code that opens the default DB) lands in a throwaway file —
     never the production DB at ~/.claude/juggle/juggle.db.

  2. `JuggleDB._connect` is wrapped to RAISE the instant any test opens the real
     production DB. Prod access in a test is then an immediate, loud error
     rather than silent pollution.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


@pytest.fixture(scope="session")
def _initialized_db_template(tmp_path_factory):
    """Build the initialized schema ONCE per pytest session (per xdist worker).

    `init_db()` is ~19ms of pure DDL + schema migrations — no per-test-unique
    wall-clock/UUID seeding (verified: it only CREATEs tables/indexes and runs
    deterministic migrations; tests asserting on DB contents build their OWN DB
    via the `db`/`juggle_db` fixtures). So the initialized file is identical for
    every test and can be materialized once and byte-copied per test
    (`shutil.copyfile` ~0.21ms) instead of re-running init_db() ~3659 times.

    Built on a SESSION-tmp path (never prod), so it never trips the prod-DB
    guard (this session fixture resolves BEFORE the per-test `_connect` guard is
    installed, so it uses the real unguarded `_connect` — harmless, the target is
    a tmp path regardless). Under xdist `-n auto`, `tmp_path_factory` is
    per-worker, so each worker builds its own template independently.

    MIGRATION AUTHORS: this optimization assumes `init_db()` (DDL + the migration
    runner) seeds NO wall-clock/UUID value that a test reads off the DEFAULT
    autouse DB. If you ever add a migration that INSERTs a `now()`/uuid row, that
    value would freeze at session-start here — either keep such rows out of
    init_db or revert this fixture to a per-test `init_db()`.
    """
    from juggle_db import JuggleDB

    template = tmp_path_factory.mktemp("db-template") / "juggle-template.db"
    JuggleDB(str(template)).init_db()
    # Fold the WAL sidecar into the main file so a plain copy is self-contained.
    # `with self._connect()` inside init_db only manages the transaction, not the
    # close, so a `-wal` may still linger — TRUNCATE-checkpoint here guarantees
    # the single-file template captures the full initialized schema.
    from juggle_db_connect import open_connection

    conn = open_connection(str(template))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    finally:
        conn.close()
    return template


@pytest.fixture(autouse=True)
def _isolate_db_from_prod(tmp_path, monkeypatch, _initialized_db_template):
    # 1. Redirect the default DB path at this test's temp dir, and initialize it
    #    so any `get_db()` / bare `JuggleDB()` with no explicit path resolves to
    #    a WORKING isolated DB (never the production DB).
    temp_db = tmp_path / "juggle-test.db"
    monkeypatch.setenv("JUGGLE_DB_PATH", str(temp_db))

    # 2. Fail-closed guard: opening the real prod DB is an immediate error.
    from juggle_db import JuggleDB

    _orig_connect = JuggleDB._connect

    def _guarded_connect(self):
        try:
            resolved = Path(self.db_path).resolve()
        except OSError:
            resolved = Path(self.db_path)
        if resolved == _PROD_DB:
            raise RuntimeError(
                "TEST ISOLATION VIOLATION: a test tried to open the production "
                f"DB {self.db_path}. Use a temp DB (tmp_path / JUGGLE_DB_PATH)."
            )
        return _orig_connect(self)

    monkeypatch.setattr(JuggleDB, "_connect", _guarded_connect)

    # Pre-initialize the isolated DB so default get_db() handles are usable.
    # Byte-copy the session template into THIS test's own tmp_path instead of
    # re-running init_db() (~19ms → ~0.21ms). Each test still gets its OWN db
    # file under its OWN tmp_path — copies never share a handle.
    shutil.copyfile(_initialized_db_template, temp_db)
    yield


# ── Spool test isolation (2026-07-02 dead-letter incident) ────────────────────
# 79 production spool events were consumed/dead-lettered by agent worktree TEST
# RUNS of test_spool_drain_idempotent.py — drain_spool/spool_dir() resolved the
# REAL ~/.juggle/spool while tests journaled into per-test tmp DBs. Two layers,
# mirroring `_isolate_db_from_prod` above:
#
#   1. `JUGGLE_SPOOL_DIR` is pointed at a per-test tmp dir, so any call to
#      `spool_dir()` (any import style — the env var is read at CALL time, so
#      this covers both `from juggle_spool_paths import spool_dir` at module
#      level and inside a function body) lands in a throwaway dir.
#
#   2. The spool read/write primitives (`dbops.spool.write_event` /
#      `read_pending` / `move_to_dead`) are wrapped to RAISE the instant any
#      test targets the real spool dir directly (bypassing spool_dir()
#      entirely) — see tests/test_spool_isolation.py.
@pytest.fixture(autouse=True)
def _isolate_spool_from_prod(tmp_path, monkeypatch):
    monkeypatch.setenv("JUGGLE_SPOOL_DIR", str(tmp_path / "spool-isolated"))

    import dbops.spool as _spool_mod
    from _spool_isolation import assert_not_prod_spool

    _orig_write_event = _spool_mod.write_event
    _orig_read_pending = _spool_mod.read_pending
    _orig_move_to_dead = _spool_mod.move_to_dead

    def _guarded_write_event(spool_dir, *a, **kw):
        assert_not_prod_spool(spool_dir)
        return _orig_write_event(spool_dir, *a, **kw)

    def _guarded_read_pending(spool_dir, *a, **kw):
        assert_not_prod_spool(spool_dir)
        return _orig_read_pending(spool_dir, *a, **kw)

    def _guarded_move_to_dead(spool_dir, *a, **kw):
        assert_not_prod_spool(spool_dir)
        return _orig_move_to_dead(spool_dir, *a, **kw)

    for _name, _fn in (
        ("write_event", _guarded_write_event),
        ("read_pending", _guarded_read_pending),
        ("move_to_dead", _guarded_move_to_dead),
    ):
        monkeypatch.setattr(_spool_mod, _name, _fn)
        # `juggle_spool_apply` imports read_pending/move_to_dead at MODULE
        # level (`from dbops.spool import ...`) — that binding is frozen at
        # import time, so patching only the dbops.spool attribute above is
        # invisible to it. Patch the local name too (mirrors the multi-module
        # worktree guard below).
        _sa_mod = sys.modules.get("juggle_spool_apply")
        if _sa_mod is not None and hasattr(_sa_mod, _name):
            monkeypatch.setattr(_sa_mod, _name, _fn)
    yield


# ── Worktree-leak guard (2026-06-20 incident) ─────────────────────────────────
# A test that called ``_create_worktree`` WITHOUT pointing ``worktree_root`` at
# its own ``tmp_path`` wrote the checkout to /private/tmp/juggle-* — outside
# pytest's managed temp dir — so it was never cleaned up. 100+ orphaned dangling
# worktrees accumulated (pruned manually 2026-06-20).
#
# This guard is HERMETIC: it wraps ``_create_worktree`` at its definition module
# and asserts, per call, that the requested ``worktree_root`` resolves under the
# test's ``tmp_path``. It only inspects calls the test under test actually makes,
# so it is immune to concurrent juggle processes (watchdog, other agents) that
# also touch the shared /tmp — a global-filesystem snapshot guard misattributes
# those to innocent tests and flakes. Tests that monkeypatch _create_worktree
# (to mock it) transparently override the wrapper for that symbol — they create
# no real worktree, so they are unaffected.


@pytest.fixture(autouse=True)
def _guard_worktree_under_tmp(tmp_path, monkeypatch):
    import juggle_cmd_agents_worktree as _wt

    _orig_create = _wt._create_worktree
    _tmp_resolved = tmp_path.resolve()

    def _guarded_create(repo_path, thread_label, worktree_root, **kwargs):
        try:
            root_resolved = Path(worktree_root).resolve()
        except OSError:
            root_resolved = Path(worktree_root)
        # The worktree root must live under THIS test's tmp_path. A bare /tmp
        # (or any path outside tmp_path) is the leak this guard exists to stop.
        if _tmp_resolved not in (root_resolved, *root_resolved.parents):
            raise AssertionError(
                "TEST HYGIENE VIOLATION (2026-06-20 worktree-leak incident): "
                f"_create_worktree called with worktree_root={worktree_root!r} "
                f"which is NOT under this test's tmp_path ({tmp_path}). Pass "
                "worktree_root=str(tmp_path) so the checkout cannot leak into /tmp."
            )
        return _orig_create(repo_path, thread_label, worktree_root, **kwargs)

    # Patch the definition module and every module that re-exports the symbol,
    # so both ``from juggle_cmd_agents_worktree import _create_worktree`` and the
    # ``juggle_cmd_agents_common._create_worktree`` production seam are guarded.
    monkeypatch.setattr(_wt, "_create_worktree", _guarded_create)
    for _modname in ("juggle_cmd_agents_common", "juggle_cmd_agents"):
        _mod = sys.modules.get(_modname)
        if _mod is not None and hasattr(_mod, "_create_worktree"):
            monkeypatch.setattr(_mod, "_create_worktree", _guarded_create)
    yield


# ── Prod-artifact guard (speedup-tier xdist safety, 2026-06-21) ───────────────
# Under `-n auto` many workers run concurrently. `_isolate_db_from_prod` already
# RAISES on a prod-DB *connection*; this guard additionally fails any test that
# tries to WRITE the prod watchdog lock or a prod pidfile.
#
# B1 (critique): the plan's original design — a global st_mtime_ns snapshot of
# the prod DB/lock/pidfile before/after each test — was REJECTED. A live watchdog
# bumps exactly those mtimes every tick, so on the dogfooding dev machine (where
# the fast inner loop most needs to be reliable) a snapshot guard flakes whenever
# a test's window straddles a tick. Instead — mirroring the hermetic
# `_guard_worktree_under_tmp` seam wrapper above — we wrap the two functions that
# WRITE a prod artifact and assert, PER CALL, that the target is not a prod path,
# BEFORE any IO. This inspects only the calls the test under test actually makes,
# so it is immune to concurrent daemons.


@pytest.fixture(autouse=True)
def _guard_no_prod_artifacts(monkeypatch):
    import daemon_pidfile as _dp
    import juggle_watchdog_singleton as _wd
    from _xdist_isolation import assert_not_prod_artifact

    _orig_lock = _wd.acquire_singleton_lock

    def _guarded_lock(db_path):
        assert_not_prod_artifact(_wd.lock_path_for(db_path))
        return _orig_lock(db_path)

    _guarded_lock._prod_artifact_guarded = True

    _orig_pid = _dp.write_singleton_pid

    def _guarded_pid(pidfile, **kwargs):
        assert_not_prod_artifact(pidfile)
        return _orig_pid(pidfile, **kwargs)

    _guarded_pid._prod_artifact_guarded = True

    monkeypatch.setattr(_wd, "acquire_singleton_lock", _guarded_lock)
    monkeypatch.setattr(_dp, "write_singleton_pid", _guarded_pid)
    yield


@pytest.fixture
def _guard_no_prod_artifacts_active(_guard_no_prod_artifacts):  # noqa: ARG001
    """Expose the guard's activeness to a pin (the autouse guard yields None)."""
    return True


# ── Agent-context cwd-heuristic neutralizer (T-spool-03, 2026-07-02) ─────────
# `dbops.graph_guards.is_agent_context()` treats ANY cwd containing
# "juggle-juggle-" as an agent/worktree process (defence in depth — see its
# docstring). Coder agents run the suite from inside exactly such a worktree
# (e.g. /tmp/juggle-juggle-AZ), so once product code started branching on
# `should_spool()` (cmd_complete_agent/cmd_fail_agent, T-spool-03) the entire
# suite silently flipped into "agent context" and spooled instead of exercising
# the real DB path it was written to test. JUGGLE_ORCHESTRATOR=1 is the
# authoritative override (wins over cwd AND JUGGLE_IS_AGENT — see
# is_agent_context()), so default every test to the orchestrator's perspective;
# tests that specifically want agent/spool behavior opt back in per-test via
# `monkeypatch.delenv("JUGGLE_ORCHESTRATOR", ...)` (see test_spool_agent_complete_fail_writes.py).
@pytest.fixture(autouse=True)
def _default_orchestrator_context(monkeypatch):
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    yield


# ── Watchdog real-daemon spawn neutralizer + survivor guard (2026-06-21 leak) ──
# The always-full-suite (v1.80) surfaced a real-daemon leak: CockpitApp.on_mount
# self-heals a detached watchdog via ensure_watchdog → a REAL
# `uv run python src/juggle_watchdog_daemon.py` against the test's tmp DB, so
# every cockpit test that drives the real app (run_test) launched a background
# daemon; if teardown reaped only the `uv run` parent the detached python child
# orphaned and kept ticking, contaminating the rest of the suite (8 full-suite
# ERRORS; observed live 2026-06-20 — TODO "Full-suite daemon teardown"). Two
# layers, both autoused:
#   1. Neutralize the spawn at its single source — set
#      `JUGGLE_WATCHDOG_DISABLE_SPAWN=1` so `ensure_watchdog` never launches a
#      real detached daemon on its real path (spawn is None). Set via setenv so
#      it ALSO propagates to `uv run` cockpit/CLI subprocess children (which
#      inherit os.environ) — the smoke / cli-memory tests run a real
#      `juggle start` / cockpit subprocess that would otherwise leak a daemon.
#      Unit tests that inject a fake `spawn=` are unaffected (only spawn-None is
#      gated).
#   2. Backstop survivor guard — after each test, fail loud (and SIGKILL) on any
#      daemon that survived holding a singleton lock under this test's own
#      tmp_path. Scoped to tmp_path so a concurrent xdist worker's daemon / the
#      live PROD watchdog is never mis-attributed (mirrors the per-seam,
#      hermetic design of the guards above).


@pytest.fixture(autouse=True)
def _no_real_watchdog_daemon_spawn(monkeypatch):
    monkeypatch.setenv("JUGGLE_WATCHDOG_DISABLE_SPAWN", "1")
    yield


@pytest.fixture(autouse=True)
def _guard_no_leaked_watchdog_daemons(tmp_path):
    yield
    from _daemon_guard import reap_survivors, scoped_daemon_survivors

    survivors = scoped_daemon_survivors(tmp_path)
    if survivors:
        reap_survivors(survivors)  # never let a leak contaminate the rest of the suite
        raise AssertionError(
            f"watchdog daemon survivor(s) {survivors} held a singleton lock under "
            f"{tmp_path} after the test (2026-06-21 daemon-teardown leak). A test "
            "that spawns a real daemon MUST reap it — kill the process GROUP "
            "(start_new_session ⇒ the daemon child is detached), SIGKILL escalation."
        )


# ── Slow-tier marking (speedup-tier B2, 2026-06-21) ───────────────────────────
# Heavy modules (cockpit/Textual render, watchdog real-daemon/gap, `uv run`
# shell-outs) are marked `slow` so the OPT-IN inner loop
# (`make test-fast` / `-m 'not slow and not watchdog_proc'`) skips them. They STAY
# in the default/integrate FULL suite — `slow` is NEVER in global addopts (B2).
#
# Marking here in ONE place (vs a `pytestmark` line in 40+ modules) is a single
# source of truth and — critically — does NOT edit the watchdog test files that
# the unlanded 2026-06-20 watchdog-hardening plan also edits, avoiding a merge
# collision (M3). When that plan's frozen-clock de-clock lands and the watchdog
# gap tests become fast, drop their suffixes from _SLOW_MODULE_SUFFIXES below.

_SLOW_MODULE_SUFFIXES = (
    "test_juggle_smoke.py",
    "test_graph_dispatch.py",
    "test_cmd_graph.py",
    "test_ensure_watchdog_debounce.py",
    "test_watchdog_daemon_main_entry.py",
    "test_integrate.py",
    "test_watchdog_freeze.py",
    # watchdog real-daemon/gap modules (coordinate w/ 2026-06-20 plan — M3):
    "watchdog/test_baseline.py",
    "watchdog/test_watchdog_active.py",
    "watchdog/test_watchdog.py",
    "watchdog/test_hardening.py",
)

# Modules that spawn a REAL `uv run` Python subprocess (watchdog daemon / CLI
# shell-out) with a wall-clock deadline. Under `-n auto`, many such cold-starts
# at once saturate the cores and the deadlines flake (2026-06-21: a 15s
# daemon-liveness wait timed out ~1/3 of runs). Route them to one xdist_group so
# at most one heavy uv-run subprocess test runs at a time — load-flake removed
# without serializing the whole suite. Honored via `--dist loadgroup`.
#
# NOTE: test_juggle_smoke.py is deliberately NOT here. It drives a real cockpit
# PTY; serial-grouping made it WORSE (it then always ran alongside test_integrate
# on one worker, at peak load → 3/3 fail). Instead its interactive frame-compare
# tests poll-until-changed (load-tolerant in-test) and stay distributed.
_SERIAL_MODULE_SUFFIXES = (
    "test_graph_dispatch.py",
    "test_cmd_graph.py",
    "test_ensure_watchdog_debounce.py",
    "test_watchdog_daemon_main_entry.py",
    "test_integrate.py",
)


# ── Config test-isolation guard (2026-07-02 real-config-mutation incident) ───
# Same isolation defect class as `_isolate_db_from_prod` above, but for
# ~/.juggle/config.json: every settings/config read (`get_settings`,
# `juggle_cmd_doctor._resolve_config_path`, `configure_db_mode`,
# `juggle_cockpit._persist_ratios`, `juggle_cockpit_topic_cap`) already honors
# a live (uncached) `_JUGGLE_CONFIG_PATH` env override, but nothing redirected
# it globally for tests — so any test (esp. a real cockpit-exit test, which
# persists column_ratios on every exit) that didn't individually patch the env
# var wrote into the real file. Concurrent agent-worktree full-suite runs
# transiently mutated live user config (harnesses.claude.readiness_markers
# momentarily missing a key), flaking
# test_juggle_harness.py::test_real_settings_default_harness_is_claude.
#
# `paths.config_dir` is a SEPARATE settings key (only `JUGGLE_CONFIG_DIR`
# overrides it, not `_JUGGLE_CONFIG_PATH`) consumed by
# `juggle_watchdog_inspect._config_dir` (snapshot dir) and
# `juggle_agent_settings._overlay_dir` (agent-settings overlays) — left
# unredirected it defaults to the real ~/.juggle too, so a watchdog test
# writing+globbing a snapshot file races the real, concurrently-running
# production watchdog daemon on the same dev machine
# (test_stalled_silent_action_item_filed flaked this way, 2026-07-02).
#
# Three fail-closed guards, mirroring `_isolate_db_from_prod`:
#   1. Redirect `_JUGGLE_CONFIG_PATH` at a per-test tmp file, and
#      `JUGGLE_CONFIG_DIR` at a per-test tmp dir. Set via setenv so both ALSO
#      propagate to `uv run` cockpit/CLI subprocess children (which inherit
#      os.environ), same as the watchdog-spawn neutralizer above.
#   2. Fail-closed guard: wrap `Path.write_text` globally to RAISE the instant
#      any test writes the real ~/.juggle/config.json (or its atomic-write
#      .tmp / .bak-pre-* siblings) — the same hermetic, per-call seam as
#      `_guard_no_prod_artifacts`. Scoped to config.json specifically (not the
#      whole ~/.juggle tree, which also holds agent-settings overlays and a
#      watchdog pidfile already covered by their OWN, unrelated guards/tests)
#      so this stays a config-isolation fix, not a broader ~/.juggle sweep.
_PROD_JUGGLE_DIR = (Path.home() / ".juggle").resolve()


def _is_prod_config_artifact(path) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return resolved.parent == _PROD_JUGGLE_DIR and resolved.name.startswith("config.json")


@pytest.fixture(autouse=True)
def _isolate_config_from_prod(tmp_path, monkeypatch):
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "juggle-test-config.json"))
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "juggle-test-config-dir"))

    _orig_write_text = Path.write_text

    def _guarded_write_text(self, *args, **kwargs):
        if _is_prod_config_artifact(self):
            raise RuntimeError(
                "TEST ISOLATION VIOLATION (2026-07-02 config-mutation incident): a "
                f"test tried to write the production config {self}. Use a temp "
                "path (tmp_path / _JUGGLE_CONFIG_PATH)."
            )
        return _orig_write_text(self, *args, **kwargs)

    _guarded_write_text._prod_artifact_guarded = True
    monkeypatch.setattr(Path, "write_text", _guarded_write_text)
    yield


@pytest.fixture
def juggle_db(tmp_path):
    """A fresh, isolated JuggleDB under tmp_path (2026-06-30 topic-graph-state-unify).

    Shared by the topic-lifecycle/derive/reconcile/forward-link test modules. Same
    shape as the local `db` fixtures in test_db_topics.py — the prod-DB guard in
    `_isolate_db_from_prod` still applies.
    """
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "juggle-fixture.db"))
    d.init_db()
    return d


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    for item in items:
        path = str(item.fspath).replace(os.sep, "/")
        fname = path.rsplit("/", 1)[-1]
        if fname.startswith("test_cockpit_") or any(
            path.endswith(suffix) for suffix in _SLOW_MODULE_SUFFIXES
        ):
            item.add_marker(pytest.mark.slow)
        if any(path.endswith(suffix) for suffix in _SERIAL_MODULE_SUFFIXES):
            item.add_marker(pytest.mark.serial)
            item.add_marker(pytest.mark.xdist_group("serial"))
