"""juggle_integrate_spawn — the shared DETACHED-integrate spawn helper.

Extracted so the two merge-landing callers share ONE spawn (2026-07-04 inline-gate
death by watchdog respawn, integrate-wedge #2):
  * the watchdog re-integrate sweep (juggle_graph_reintegrate), and
  * complete-agent (juggle_cmd_agents_complete) at completion time.

Neither may EVER run the ~7-min merge gate inline: the sweep runs on the watchdog
tick (a long inline gate trips the tickguard budget and livelocks the daemon), and
complete-agent's completion is spool-applied INSIDE the watchdog process, which
self-restarts on every plugin-HEAD advance — and every successful integrate
advances HEAD, so an inline gate is routinely killed mid-run. ``start_new_session``
detaches the child from the watchdog's process group so a restart can't kill it;
the per-repo integrate lock + heartbeat serialize concurrent spawns (single-flight)
even across a restart; ``JUGGLE_ORCHESTRATOR=1`` marks it watchdog-owned so the
integrate guard permits the call. Its outcome reconciles on a LATER watchdog tick.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("juggle-integrate-spawn")


def spawn_detached_integrate(thread: dict, db):
    """Spawn ``juggle integrate <thread>`` DETACHED; return its Popen handle
    (None on missing fields / spawn failure). NEVER blocks the caller."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    thread_id = (thread.get("id") or "").strip()
    repo = (thread.get("main_repo_path") or "").strip()
    if not thread_id or not repo:
        return None

    cli = str(Path(__file__).resolve().parent / "juggle_cli.py")
    env = os.environ.copy()
    env["JUGGLE_ORCHESTRATOR"] = "1"  # integrate is watchdog-owned; this IS the watchdog
    db_path = getattr(db, "db_path", None)
    if db_path:
        env["JUGGLE_DB_PATH"] = str(db_path)

    # Detached-process output → a durable log (the incident was diagnosed from
    # the spawn log); fall back to DEVNULL if the dir is unwritable.
    logf = subprocess.DEVNULL
    try:
        log_dir = Path(db_path).parent if db_path else Path(repo)
        log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(log_dir / "reintegrate-spawn.log", "ab")
    except OSError:
        pass
    try:
        proc = subprocess.Popen(
            [sys.executable, cli, "integrate", thread_id], cwd=repo, env=env,
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
        )
    except Exception:
        _log.exception("failed to spawn detached integrate for %s", thread_id)
        return None
    finally:
        if logf is not subprocess.DEVNULL:
            logf.close()  # child dup'd the fd
    return proc
