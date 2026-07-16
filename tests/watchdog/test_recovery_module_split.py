"""Module-boundary pin for the N6 slimming extraction (2026-07-15).

execute_recovery must live in the new flat juggle_watchdog_recovery.py,
re-exported by juggle_watchdog.py for backward-compat (same pattern as
juggle_watchdog_inspect/_restart/_paneparse/_orphans).
RED before the extraction: juggle_watchdog_recovery doesn't exist yet.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def test_execute_recovery_lives_in_recovery_module():
    from juggle_watchdog_recovery import execute_recovery

    assert execute_recovery.__module__ == "juggle_watchdog_recovery"


def test_juggle_watchdog_reexports_same_object():
    import juggle_watchdog
    from juggle_watchdog_recovery import execute_recovery

    assert juggle_watchdog.execute_recovery is execute_recovery
