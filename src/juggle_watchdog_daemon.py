"""Watchdog daemon loop — library logic behind scripts/juggle-agent-watchdog.

Owns the 30s polling tick (`_poll_once`: heartbeat → classify each busy agent's
pane → prompt-handling / stuck-Enter retries / recovery escalation → orphan
check → stale-agent reap), the singleton-pidfile bootstrap, signal handling,
and the hot-restart staleness exit. It must not own classification or recovery
policy itself — those live in juggle_watchdog / juggle_watchdog_restart /
juggle_watchdog_inspect. Entry point: ``main()``, invoked by the thin
``scripts/juggle-agent-watchdog`` wrapper.
"""
from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import daemon_pidfile
from juggle_db import JuggleDB, DB_PATH
from juggle_settings import get_settings
from juggle_tmux import JuggleTmuxManager
from juggle_watchdog_health import write_heartbeat
from juggle_watchdog import (
    _kill_existing_watchdog_from_pidfile,
    check_orphaned_threads,
    classify_pane_state,
    execute_recovery,
    get_session_id,
    get_threshold_seconds,
    handle_prompt,
    read_snapshot,
    write_snapshot,
)

# Repo root captured at IMPORT time (before the main() chdir to ~/.juggle) so
# git-HEAD staleness checks resolve the canonical main worktree, not the cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

_POLL_INTERVAL = int(os.environ.get("JUGGLE_WATCHDOG_INTERVAL", "30"))
_SUPERVISED = os.environ.get("JUGGLE_WATCHDOG_SUPERVISED", "") == "1"
_SUPERVISOR_PID: int | None = None
try:
    _spid = os.environ.get("JUGGLE_SUPERVISOR_PID")
    if _spid:
        _SUPERVISOR_PID = int(_spid)
except (TypeError, ValueError):
    pass


def should_exit_supervisor_gone(supervisor_pid_alive: bool) -> bool:
    """Return True when the supervising cockpit process is gone."""
    return not supervisor_pid_alive


def _is_pid_alive_local(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# Single authoritative pidfile — no more duplicate config_dir/watchdog.pid
SINGLETON_PID_FILE = Path.home() / ".juggle" / "watchdog.pid"
_JUGGLE_DIR = Path.home() / ".juggle"

# Boot-HEAD sidecar (2026-07-01 churn fix): the live daemon records the git HEAD
# it booted on so a near-simultaneous respawn can tell whether the incumbent is a
# fresh SAME-code peer (defer to it) or an OLD-code instance (kill and replace).
SINGLETON_CODEVERSION_FILE = _JUGGLE_DIR / "watchdog.codeversion"


def _write_singleton_pid():
    # Atomic write + race verification (exits 1 if another start claimed the file)
    daemon_pidfile.write_singleton_pid(SINGLETON_PID_FILE, verify=True, name="watchdog")


def _read_incumbent_code_version() -> str | None:
    """Boot HEAD recorded by the daemon currently holding the singleton, or None."""
    try:
        v = SINGLETON_CODEVERSION_FILE.read_text().strip()
        return v or None
    except OSError:
        return None


def _record_boot_code_version(version: str | None) -> None:
    """Publish our boot HEAD so a racing respawn can classify us as same/old code."""
    if version is None:
        return
    try:
        SINGLETON_CODEVERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SINGLETON_CODEVERSION_FILE.write_text(version)
    except OSError:
        pass


def _reconcile_existing_watchdog(
    boot_code_version: str | None, *, stale_after: float | None = None
) -> None:
    """Idempotent startup reconciliation (2026-07-01 respawn-churn fix).

    Replaces the old unconditional ``_kill_existing_watchdog_from_pidfile`` call.
    Kills the pidfile incumbent ONLY when it runs OLD code or is HUNG; a fresh,
    same-code, live incumbent is left alone so the flock (acquired next) makes
    this redundant newcomer exit — instead of the newcomer SIGTERM'ing a healthy
    fresh peer and driving the restart storm.
    """
    from juggle_watchdog_health import read_heartbeat_age
    from juggle_watchdog_restart import should_replace_incumbent

    if stale_after is None:
        stale_after = float(
            get_settings().get("watchdog", {}).get("hung_heartbeat_secs", 120)
        )
    incumbent_version = _read_incumbent_code_version()
    if boot_code_version is None or incumbent_version is None:
        same_code: bool | None = None
    else:
        same_code = incumbent_version == boot_code_version
    heartbeat_age = read_heartbeat_age()
    if should_replace_incumbent(
        same_code=same_code, heartbeat_age=heartbeat_age, stale_after=stale_after
    ):
        _kill_existing_watchdog_from_pidfile(SINGLETON_PID_FILE)
    else:
        _log.info(
            "Watchdog: fresh same-code instance already live (incumbent HEAD=%s) "
            "— deferring to it, not respawning (idempotent)",
            incumbent_version,
        )


def _cleanup_singleton_pid():
    daemon_pidfile.cleanup_singleton_pid(SINGLETON_PID_FILE)


def _release_singleton_lock(fd):
    from juggle_watchdog_singleton import release_singleton_lock
    release_singleton_lock(fd)


def prune_stale_watchdog_pidfiles(juggle_dir: Path | None = None) -> None:
    """Remove watchdog-*.pid files whose recorded PID is no longer alive.

    Keeps the current process's pidfile and any file belonging to a live PID.
    Only touches files matching the watchdog-*.pid glob — never monitor.pid or
    other non-watchdog files.
    """
    directory = juggle_dir if juggle_dir is not None else _JUGGLE_DIR
    for pidfile in directory.glob("watchdog-*.pid"):
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            # Corrupt or unreadable — treat as stale
            try:
                pidfile.unlink()
            except OSError:
                pass
            continue
        # kill -0: check if process is alive without sending a real signal
        try:
            os.kill(pid, 0)
            # Process alive — keep the file
        except (ProcessLookupError, PermissionError):
            # ProcessLookupError: no such process → stale
            # PermissionError: exists but we can't signal it (still alive)
            if isinstance(sys.exc_info()[1], ProcessLookupError):
                try:
                    pidfile.unlink()
                    _log.info("Pruned stale watchdog pidfile %s (PID %d gone)", pidfile.name, pid)
                except OSError:
                    pass


_log = logging.getLogger("juggle-watchdog")


def _setup_logging() -> None:
    """Configure root logging (file + stdout). Called from main() so importing
    this module has no filesystem side effects."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(
                Path(get_settings()["paths"]["config_dir"]) / "watchdog.log"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


_running = True
# In-memory Enter retry count; resets on restart (stalled-silent is fallback)
_enter_sent: dict[str, int] = {}

# Stalled-pane detector state (busy agents idling at the prompt). Lives across
# ticks like _enter_sent; resets on daemon restart. See juggle_watchdog_stall.
from juggle_watchdog_stall import StallTracker as _StallTracker  # noqa: E402
_stall_tracker = _StallTracker()

# P4 tick-on-demand: threading.Event coalesces N concurrent SIGUSR1 signals
# into at most one extra tick. The handler ONLY sets the event (async-signal-safe
# in CPython: GIL + threading.Event.set() is lock-free in the signal context).
# All DB/tmux work happens in the main loop thread after event.wait() returns.
_tick_event = threading.Event()


def _handle_sigusr1(signum, frame):
    _tick_event.set()  # idempotent; coalesces multiple concurrent signals


def _handle_sigterm(signum, frame):
    global _running
    _log.info("Watchdog: SIGTERM received, shutting down")
    _running = False
    _tick_event.set()  # unblock event.wait() so the loop exits promptly


def _get_dirs() -> tuple[Path, Path]:
    config_dir = Path(get_settings()["paths"]["config_dir"])
    return (
        config_dir / "watchdog" / "snapshots",
        config_dir / "watchdog" / "recovery",
    )


def _capture_pane(mgr: JuggleTmuxManager, pane_id: str, lines: int = 80) -> str | None:
    if not mgr.verify_pane(pane_id):
        return None
    result = mgr._run_tmux("capture-pane", "-pt", pane_id, "-S", f"-{lines}")
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _poll_once(db: JuggleDB, mgr: JuggleTmuxManager) -> None:
    write_heartbeat()
    snapshot_dir, recovery_dir = _get_dirs()
    now_ts = time.time()
    session_id = get_session_id(db)
    agents = [a for a in db.get_all_agents() if a["status"] == "busy"]

    for agent in agents:
        agent_id = agent["id"]
        pane_id = agent["pane_id"]

        prev = read_snapshot(agent_id, snapshot_dir)
        content = _capture_pane(mgr, pane_id)

        # Compute stalled_for from last_activity_at in DB (survives restarts)
        last_activity_at_str = agent.get("last_activity_at")
        if last_activity_at_str:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(last_activity_at_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                stalled_for = now_ts - last_dt.timestamp()
            except (ValueError, TypeError):
                stalled_for = 0.0
        else:
            stalled_for = 0.0  # first observation — treat as just changed

        threshold = get_threshold_seconds(db, agent)

        state, key = classify_pane_state(
            content=content,
            prev_content=prev,
            stalled_for=stalled_for,
            threshold=threshold,
            last_send_task_pane_hash=agent.get("last_send_task_pane_hash"),
            last_send_task_at=agent.get("last_send_task_at"),
        )

        if state == "working":
            write_snapshot(agent_id, content, snapshot_dir)
            from datetime import datetime, timezone
            db.update_agent(agent_id, last_activity_at=datetime.now(timezone.utc).isoformat())
            _enter_sent.pop(agent_id, None)

        elif state == "prompt":
            handle_prompt(db, mgr, agent, pane_id, key or "")
            write_snapshot(agent_id, content, snapshot_dir)
            from datetime import datetime, timezone
            db.update_agent(agent_id, last_activity_at=datetime.now(timezone.utc).isoformat())
            _enter_sent.pop(agent_id, None)

        elif state == "stuck":
            enter_count = _enter_sent.get(agent_id, 0)
            if enter_count < 2:
                mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
                _enter_sent[agent_id] = enter_count + 1
                db.add_notification_v2(
                    thread_id=agent.get("assigned_thread"),
                    message=(f"[Watchdog] agent {agent_id[:8]} stuck-at-prompt — "
                             f"sent Enter (attempt {enter_count + 1}/2)"),
                    session_id=session_id,
                )
                _log.info("Watchdog: stuck-at-prompt Enter #%d sent to %s",
                          enter_count + 1, agent_id[:8])
            else:
                _log.warning("Watchdog: agent %s stuck after 2 Enters — escalating to recovery",
                             agent_id[:8])
                execute_recovery(db, mgr, agent, content or "",
                                 recovery_dir=recovery_dir, session_id=session_id)
                _enter_sent.pop(agent_id, None)

        elif state in ("stalled", "crashed"):
            _log.warning("Watchdog: agent %s is %s (stalled_for=%.0fs threshold=%.0fs)",
                         agent_id[:8], state, stalled_for, threshold)
            execute_recovery(db, mgr, agent, content or "",
                             recovery_dir=recovery_dir, session_id=session_id)

        elif state == "awaiting_dispatch":
            _log.info("Watchdog: agent %s awaiting first dispatch — skipping recovery",
                      agent_id[:8])

        # "quiet" — no action

    # Loop 1b: stalled-pane detector — nudge busy agents idling at the prompt
    # (finished work but never finalized). Guarded so a bug never downs the tick.
    try:
        from juggle_watchdog_stall import check_stalled_agents
        check_stalled_agents(db, mgr, _stall_tracker, now=now_ts, session_id=session_id)
    except Exception:
        _log.exception("Watchdog: stall detector tick failed — continuing")

    # Loop 2: orphaned thread detection
    _orphan_threshold = float(os.environ.get("JUGGLE_ORPHAN_THRESHOLD", "300"))
    check_orphaned_threads(db, orphan_threshold=_orphan_threshold)

    # Loop 2b: completed-but-unmerged topic guard (G5) — surface any topic whose
    # tasks are all verified but whose work never merged (close-before-integrate
    # orphan) so it is never silently abandoned without a blocker.
    try:
        from dbops.orphan_guard import flag_unmerged_completed_topics
        flag_unmerged_completed_topics(db)
    except Exception:
        pass

    # Generic stale-agent reap (pass 1: idle TTL / dead panes; pass 2: orphan panes)
    from juggle_tmux import reap_stale_agents
    try:
        reap_stale_agents(db, mgr)
    except Exception:
        pass

    # Watchdog-daemon reaper (2026-06-20 leak): reap orphans + global daemon cap.
    # ONLY the sanctioned prod daemon polices the system-wide (pgrep) table — a
    # non-prod daemon must never SIGTERM a process it did not spawn (2026-07-01
    # isolation: test daemons killed the live prod watchdog via the cap). Fail-safe.
    try:
        from juggle_reaper import reap_watchdog_daemons_tick
        from juggle_watchdog_singleton import is_prod_db
        if is_prod_db(DB_PATH):
            reap_watchdog_daemons_tick(int(get_settings().get("watchdog", {}).get("max_daemons", 8)))
    except Exception:
        _log.exception("Watchdog: daemon reaper tick failed — continuing")

    # Graph claim-dispatch tick (autopilot Phase 2): SOLE dispatcher for armed
    # projects' ready tasks (DA B4/M1). Guarded so a tick bug never downs the daemon.
    try:
        from juggle_graph_dispatch import graph_tick
        graph_tick(db, mgr)
    except Exception:
        _log.exception("Watchdog: graph dispatch tick failed — continuing")
    from juggle_topic_reconcile import tick_sweep as _ts  # F5 conversation-topic sweep
    _ts(db)

    # Self-heal auto-diagnosis: fire-and-forget, never block the tick.
    try:
        from juggle_selfheal import maybe_dispatch_selfheal_diagnosis
        maybe_dispatch_selfheal_diagnosis(db)
    except Exception:
        _log.exception("Watchdog: selfheal diagnosis tick failed — continuing")


def _set_orchestrator_preamble() -> None:
    """Mark this process as the orchestrator and chdir to a stable non-worktree dir.

    Must be called at the very top of main() before any DB access.  Belt-and-
    suspenders against G2 (is_agent_context cwd heuristic): the watchdog may be
    spawned while the launching shell's cwd is inside a juggle worktree
    (/tmp/juggle-juggle-*), which would cause SharedDBMigrationRefused on
    init_db().  JUGGLE_ORCHESTRATOR=1 is the authoritative override; the chdir
    is a second line of defence in case the env var is somehow unset.
    """
    os.environ["JUGGLE_ORCHESTRATOR"] = "1"
    safe_dir = DB_PATH.parent  # ~/.juggle — always stable, never a worktree
    try:
        os.chdir(safe_dir)
    except OSError:
        pass  # non-fatal; env marker is the primary guard


def main() -> None:
    from juggle_watchdog_restart import (
        current_code_version,
        should_exit_for_stale_code,
    )

    from juggle_watchdog_singleton import (
        WatchdogAlreadyRunning,
        WatchdogLaunchRefused,
        acquire_singleton_lock,
        assert_launch_allowed,
    )

    _set_orchestrator_preamble()
    _setup_logging()

    # G3 (2026-06-16 incident): a worktree/test-launched daemon must NEVER tick
    # against the production DB. Only the sanctioned orchestrator entrypoint sets
    # JUGGLE_WATCHDOG_SANCTIONED — its absence aborts a prod-targeted launch.
    try:
        assert_launch_allowed(DB_PATH)
    except WatchdogLaunchRefused as exc:
        _log.error("Watchdog: %s", exc)
        sys.exit(1)

    mode = "supervised (launchd)" if _SUPERVISED else "unsupervised"
    _log.info("Watchdog starting (PID=%d, interval=%ds, mode=%s)",
              os.getpid(), _POLL_INTERVAL, mode)

    # Fingerprint our boot HEAD up front — used both by the idempotent respawn
    # reconciliation below and by the hot-restart staleness check in the loop.
    _boot_code_version = current_code_version(_REPO_ROOT)

    prune_stale_watchdog_pidfiles()

    # Idempotent respawn (2026-07-01 churn fix): only kill the pidfile incumbent
    # when it runs OLD code or is hung. A fresh SAME-code peer is left alone so
    # the flock below (not a mutual SIGTERM) resolves the race to one survivor.
    _reconcile_existing_watchdog(_boot_code_version)

    # Exclusive singleton flock per DB: refuse to become a second concurrent
    # watchdog. When a fresh same-code peer already holds it, this newcomer is the
    # redundant loser and exits cleanly here — no restart storm.
    try:
        _singleton_fd = acquire_singleton_lock(DB_PATH)
    except WatchdogAlreadyRunning as exc:
        _log.error("Watchdog: %s — refusing to start a second instance", exc)
        sys.exit(1)
    atexit.register(_release_singleton_lock, _singleton_fd)

    # Publish boot state BEFORE the pidfile: any peer that later reads our PID
    # from the pidfile is then guaranteed to also see a fresh heartbeat and our
    # boot HEAD, so it classifies us as a fresh same-code peer and defers.
    _record_boot_code_version(_boot_code_version)
    write_heartbeat()
    _write_singleton_pid()
    atexit.register(_cleanup_singleton_pid)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    signal.signal(signal.SIGUSR1, _handle_sigusr1)

    try:
        db = JuggleDB(str(DB_PATH))
        db.init_db()
        db.cleanup_watchdog_events()

        mgr = JuggleTmuxManager()
        _log.info("Watchdog ready (PID=%d, interval=%ds, supervised=%s)",
                  os.getpid(), _POLL_INTERVAL, _SUPERVISED)

        # Defect B (2026-07-01): boot HEAD (fingerprinted above) drives the
        # hot-restart check; on drift, exit cleanly at loop TOP (never mid-tick)
        # → cockpit respawns on fresh code.
        while _running:
            cur_code_version = current_code_version(_REPO_ROOT)
            if should_exit_for_stale_code(_boot_code_version, cur_code_version):
                _log.warning(
                    "Watchdog: plugin code advanced (HEAD %s → %s) — exiting "
                    "cleanly for respawn on fresh code",
                    _boot_code_version, cur_code_version,
                )
                break  # falls through to finally: pidfile cleanup + lock release
            if _SUPERVISOR_PID is not None:
                if should_exit_supervisor_gone(_is_pid_alive_local(_SUPERVISOR_PID)):
                    _log.info(
                        "Watchdog: supervisor PID=%d gone — exiting cleanly",
                        _SUPERVISOR_PID,
                    )
                    break
            # P4: wait up to _POLL_INTERVAL for a SIGUSR1 poke or timeout.
            # Clear BEFORE _poll_once so a signal arriving during the tick
            # sets the event again → exactly one follow-up tick (coalescing).
            _tick_event.wait(timeout=_POLL_INTERVAL)
            _tick_event.clear()
            if not _running:
                break
            try:
                _poll_once(db, mgr)
            except Exception as exc:
                _log.exception("Watchdog: unhandled error in poll — continuing")
                try:
                    from juggle_selfheal import record_error
                    record_error(exc, "juggle_watchdog.poll")
                except Exception:
                    pass
    finally:
        _cleanup_singleton_pid()
        _log.info("Watchdog stopped.")


if __name__ == "__main__":
    main()
