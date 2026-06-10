"""juggle_cockpit_profile — Headless profiling harness for the cockpit.

Owns: _parse_psrecord_log, _profile_worker_loop, run_profile.
These functions run a headless snapshot+render loop and record CPU/RSS
metrics via psrecord. They are only used by the --profile CLI flag and
are completely independent of the Textual App.
Must not own: CockpitApp, layout helpers, column-ratio logic.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def _parse_psrecord_log(log_text: str) -> dict:
    """Parse a psrecord log and return summary stats.

    psrecord log format::

        # Elapsed time   CPU (%)     Real (MB)   Virtual (MB)
        0.000            5.0         100.0       500.0
        ...

    Returns a dict with keys: avg_cpu, peak_cpu, rss_start, rss_end,
    rss_growth, peak_rss.  Returns ``{}`` if no data rows are found.
    """
    cpu_vals: list[float] = []
    rss_vals: list[float] = []

    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            try:
                cpu_vals.append(float(parts[1]))
                rss_vals.append(float(parts[2]))
            except ValueError:
                continue

    if not cpu_vals:
        return {}

    return {
        "avg_cpu": sum(cpu_vals) / len(cpu_vals),
        "peak_cpu": max(cpu_vals),
        "rss_start": rss_vals[0],
        "rss_end": rss_vals[-1],
        "rss_growth": rss_vals[-1] - rss_vals[0],
        "peak_rss": max(rss_vals),
    }


def _profile_worker_loop(
    duration: int,
    db_path: str | None = None,
    _tick_fn=None,
) -> int:
    """Run a headless snapshot+render loop for *duration* seconds.

    Each iteration calls ``snapshot(db)`` + ``render_static_from_state`` — the
    same work as the live 1-second tick — without a TTY or Textual App.

    Parameters
    ----------
    duration:
        How many seconds to run.
    db_path:
        Optional path to juggle.db.
    _tick_fn:
        Replacement tick callable (injected by tests).  When ``None`` the
        real snapshot+render cycle is used.

    Returns
    -------
    int
        Number of completed iterations.
    """
    if _tick_fn is not None:
        tick_callable = _tick_fn
    else:
        from juggle_cockpit_model import snapshot as _snapshot
        from juggle_cockpit_static import render_static_from_state
        from juggle_cockpit import _make_cockpit_db

        db = _make_cockpit_db(db_path)

        def _default_tick() -> None:
            state = _snapshot(db)
            render_static_from_state(state)

        tick_callable = _default_tick

    end = time.time() + duration
    iterations = 0
    while time.time() < end:
        tick_start = time.time()
        tick_callable()
        iterations += 1
        elapsed = time.time() - tick_start
        sleep_time = max(0.0, 1.0 - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
    return iterations


def run_profile(duration: int = 60, db_path: str | None = None) -> None:
    """Run the cockpit profiling harness.

    Spawns a headless worker child (``--profile-worker``) that mimics the live
    1-second cockpit tick for *duration* seconds.  Concurrently, ``psrecord``
    (via ``uvx``) samples the child's CPU and RSS every 0.5 s.  After both
    finish the log is parsed and a summary printed to stdout.

    Degrades gracefully if ``uvx``/``psrecord`` are unavailable — exits 0 with
    a clear message so CI is not broken.
    """
    import os as _os

    log_path = Path("/tmp/cockpit_profile.log")
    plot_path = Path("/tmp/cockpit_profile.png")

    # Locate the juggle_cockpit entry-point script relative to this file.
    cockpit_script = str(Path(__file__).parent / "juggle_cockpit.py")
    worker_cmd = [
        "uv", "run", cockpit_script,
        "--profile-worker",
        "--duration", str(duration),
    ]
    if db_path:
        worker_cmd += ["--db", db_path]

    print(f"[profile] Starting headless worker ({duration}s) …", flush=True)
    try:
        child = subprocess.Popen(
            worker_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("ERROR: 'uv' not found — cannot spawn worker process.", file=sys.stderr)
        sys.exit(1)

    pid = child.pid
    print(f"[profile] Worker PID: {pid}", flush=True)

    # --- start psrecord via uvx --------------------------------------------
    psrecord_cmd = [
        "uvx", "psrecord", str(pid),
        "--interval", "0.5",
        "--duration", str(duration + 2),
        "--plot", str(plot_path),
        "--log", str(log_path),
    ]
    print(f"[profile] Running: {' '.join(psrecord_cmd)}", flush=True)
    psrecord_ok = True
    psrecord_proc: subprocess.Popen | None = None
    try:
        psrecord_proc = subprocess.Popen(
            psrecord_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        psrecord_ok = False
        print("[profile] WARNING: 'uvx' not found — skipping psrecord sampling.", flush=True)

    # --- wait for worker ---------------------------------------------------
    try:
        child.wait(timeout=duration + 15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()

    # --- wait for psrecord -------------------------------------------------
    if psrecord_ok and psrecord_proc is not None:
        try:
            psrecord_proc.wait(timeout=duration + 15)
        except subprocess.TimeoutExpired:
            psrecord_proc.kill()
            psrecord_proc.wait()

    # --- print summary -----------------------------------------------------
    if not psrecord_ok or not log_path.exists():
        print(
            "\n[profile] psrecord log not available"
            " (uvx/psrecord not installed or failed).",
            flush=True,
        )
        print("[profile] Install: pip install psrecord  (no restart needed)", flush=True)
        print("[profile] Profiling run complete (no metrics collected).", flush=True)
        return

    try:
        log_text = log_path.read_text()
    except OSError as exc:
        print(f"[profile] ERROR reading log: {exc}", file=sys.stderr)
        sys.exit(1)

    stats = _parse_psrecord_log(log_text)
    if not stats:
        print("[profile] WARNING: psrecord log is empty or unparseable.", flush=True)
        return

    w = 52
    print(f"\n{'=' * w}")
    print("  Cockpit Profile Summary")
    print(f"{'=' * w}")
    print(f"  CPU avg:    {stats['avg_cpu']:.1f}%")
    print(f"  CPU peak:   {stats['peak_cpu']:.1f}%")
    print(f"  RSS start:  {stats['rss_start']:.1f} MB")
    print(f"  RSS end:    {stats['rss_end']:.1f} MB")
    print(f"  RSS growth: {stats['rss_growth']:+.1f} MB")
    print(f"  RSS peak:   {stats['peak_rss']:.1f} MB")
    print(f"{'=' * w}")

    if stats["rss_growth"] > 20.0:
        print(
            f"  ⚠  POSSIBLE LEAK: RSS grew {stats['rss_growth']:.1f} MB"
            f" (threshold: 20 MB)"
        )
    if stats["avg_cpu"] > 15.0:
        print(
            f"  ⚠  BATTERY CONCERN: avg CPU {stats['avg_cpu']:.1f}%"
            f" (threshold: 15%)"
        )

    print(f"\n  Plot: {plot_path}")
