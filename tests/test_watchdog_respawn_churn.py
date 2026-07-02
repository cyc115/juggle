"""Regression pins — watchdog respawn churn (2026-07-01 incident T-fix-watchdog-respawn-churn).

Incident (2026-07-01 ~23:52-23:53): after a plugin code-advance exit (by design),
multiple concurrent CLI invocations each respawned the watchdog and each new
daemon, on startup, unconditionally SIGTERM'd the previous instance recorded in
the pidfile (log: 'killed previous instance (PID …)') — 4 restarts in ~64s. The
startup kill ran BEFORE the singleton flock acquire, so it defeated the flock:
two near-simultaneous SAME-CODE fresh daemons mutually killed each other.

Fix: make the startup reconciliation idempotent. Only kill-and-replace the
pidfile incumbent when it is running OLD code (different boot HEAD) or is hung
(stale heartbeat). A fresh, same-code, live watchdog is left untouched — the
flock then makes the redundant newcomer exit, so exactly one daemon survives and
zero SIGTERMs are sent to a fresh same-code instance.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


# ── 1. Pure decision: should_replace_incumbent ────────────────────────────────


def test_should_replace_incumbent_same_code_fresh_never_replaces():
    """A live, same-code incumbent with a fresh heartbeat is NEVER replaced —
    this is the churn fix: two same-code respawns must not SIGTERM each other."""
    from juggle_watchdog_restart import should_replace_incumbent

    assert should_replace_incumbent(
        same_code=True, heartbeat_age=1.0, stale_after=120
    ) is False


def test_should_replace_incumbent_old_code_replaces():
    """An incumbent on OLD code (different boot HEAD) is replaced — the newcomer
    carries fresher merged code."""
    from juggle_watchdog_restart import should_replace_incumbent

    assert should_replace_incumbent(
        same_code=False, heartbeat_age=1.0, stale_after=120
    ) is True


def test_should_replace_incumbent_hung_same_code_replaces():
    """A same-code incumbent that stopped ticking (stale heartbeat) is hung —
    replace it to recover."""
    from juggle_watchdog_restart import should_replace_incumbent

    assert should_replace_incumbent(
        same_code=True, heartbeat_age=999.0, stale_after=120
    ) is True


def test_should_replace_incumbent_unknown_code_fresh_defers():
    """When the incumbent's code version can't be determined (same_code None) but
    its heartbeat is fresh, defer to it — never kill a live instance we can't
    classify. Fail-safe toward NOT churning."""
    from juggle_watchdog_restart import should_replace_incumbent

    assert should_replace_incumbent(
        same_code=None, heartbeat_age=1.0, stale_after=120
    ) is False


def test_should_replace_incumbent_no_heartbeat_same_code_defers():
    """No heartbeat yet (None) but same code → still a fresh peer, defer."""
    from juggle_watchdog_restart import should_replace_incumbent

    assert should_replace_incumbent(
        same_code=True, heartbeat_age=None, stale_after=120
    ) is False


# ── 2. Startup reconciliation is idempotent (no self-SIGTERM churn) ───────────


def _setup_reconcile(tmp_path):
    """Isolated pidfile + codeversion sidecar and a SIGTERM-recording kill spy."""
    import juggle_watchdog_respawn as respawn

    pidfile = tmp_path / "watchdog.pid"
    codever = tmp_path / "watchdog.codeversion"
    kills: list = []
    return respawn, pidfile, codever, kills, kills.append


def test_reconcile_skips_kill_for_fresh_same_code_incumbent(tmp_path):
    """CHURN PIN: a second daemon booting on the SAME code sees the pidfile
    incumbent's recorded HEAD == its own boot HEAD and a fresh heartbeat, so it
    sends ZERO SIGTERMs — it defers to the flock instead."""
    respawn, pidfile, codever, kills, kill_fn = _setup_reconcile(tmp_path)

    pidfile.write_text("4242")           # incumbent PID
    codever.write_text("headSHA")        # incumbent booted on this HEAD

    killed = respawn.reconcile_existing_watchdog(
        "headSHA", pidfile=pidfile, kill_fn=kill_fn,
        heartbeat_age=2.0, stale_after=120, codeversion_path=codever,
    )

    assert killed is False and kills == [], "fresh same-code incumbent must never be SIGTERM'd"


def test_reconcile_kills_old_code_incumbent(tmp_path):
    """An incumbent recorded on a DIFFERENT (older) HEAD is killed and replaced."""
    respawn, pidfile, codever, kills, kill_fn = _setup_reconcile(tmp_path)

    pidfile.write_text("4242")
    codever.write_text("oldSHA")

    killed = respawn.reconcile_existing_watchdog(
        "newSHA", pidfile=pidfile, kill_fn=kill_fn,
        heartbeat_age=2.0, stale_after=120, codeversion_path=codever,
    )

    assert killed is True and kills == [pidfile], "old-code incumbent must be killed and replaced"


def test_reconcile_kills_hung_same_code_incumbent(tmp_path):
    """A same-code incumbent that stopped heartbeating (hung) is killed."""
    respawn, pidfile, codever, kills, kill_fn = _setup_reconcile(tmp_path)

    pidfile.write_text("4242")
    codever.write_text("headSHA")

    killed = respawn.reconcile_existing_watchdog(
        "headSHA", pidfile=pidfile, kill_fn=kill_fn,
        heartbeat_age=999.0, stale_after=120, codeversion_path=codever,
    )

    assert killed is True and kills == [pidfile], "hung same-code incumbent must be killed and replaced"


def test_reconcile_unknown_incumbent_version_fresh_defers(tmp_path):
    """No codeversion sidecar (incumbent HEAD unknown) but a fresh heartbeat →
    defer; never churn a live instance we can't classify."""
    respawn, pidfile, codever, kills, kill_fn = _setup_reconcile(tmp_path)

    pidfile.write_text("4242")  # codever sidecar absent

    killed = respawn.reconcile_existing_watchdog(
        "headSHA", pidfile=pidfile, kill_fn=kill_fn,
        heartbeat_age=2.0, stale_after=120, codeversion_path=codever,
    )

    assert killed is False and kills == []


def test_two_near_simultaneous_respawns_yield_one_survivor(tmp_path):
    """End-to-end churn pin: incumbent D1 is fully up (pidfile + codeversion +
    fresh heartbeat). A near-simultaneous newcomer D2 on the SAME code reconciles
    → ZERO kills, so D1 survives and the flock would make D2 exit as the loser.
    Exactly one surviving daemon, zero SIGTERMs of a fresh same-code instance."""
    respawn, pidfile, codever, sigterms, kill_fn = _setup_reconcile(tmp_path)

    # D1 came up first and published its singleton state.
    pidfile.write_text("1001")
    codever.write_text("SAME_HEAD")

    # D2 boots on the SAME code and reconciles before trying the flock.
    killed = respawn.reconcile_existing_watchdog(
        "SAME_HEAD", pidfile=pidfile, kill_fn=kill_fn,
        heartbeat_age=3.0, stale_after=120, codeversion_path=codever,
    )

    assert killed is False and sigterms == [], (
        "a fresh same-code respawn must send zero SIGTERMs — D1 survives, "
        "D2 defers to the flock (exactly one daemon)"
    )


def test_record_and_read_boot_code_version_roundtrip(tmp_path):
    """record_boot_code_version publishes the boot HEAD; read_incumbent reads it
    back. A None version is a no-op (never writes an empty sidecar)."""
    import juggle_watchdog_respawn as respawn

    sidecar = tmp_path / "watchdog.codeversion"
    respawn.record_boot_code_version("abc123", path=sidecar)
    assert respawn.read_incumbent_code_version(sidecar) == "abc123"

    respawn.record_boot_code_version(None, path=tmp_path / "absent.codeversion")
    assert respawn.read_incumbent_code_version(tmp_path / "absent.codeversion") is None
