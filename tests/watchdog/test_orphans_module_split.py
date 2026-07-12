"""Module-boundary pin for the N5 slimming extraction (2026-07-12).

check_orphaned_threads must live in the new flat juggle_watchdog_orphans.py,
re-exported by juggle_watchdog.py for backward-compat (same pattern as
juggle_watchdog_inspect/_restart/_paneparse). RED before the extraction:
juggle_watchdog_orphans doesn't exist yet.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def test_check_orphaned_threads_lives_in_orphans_module():
    from juggle_watchdog_orphans import check_orphaned_threads

    assert check_orphaned_threads.__module__ == "juggle_watchdog_orphans"


def test_juggle_watchdog_reexports_same_object():
    import juggle_watchdog
    from juggle_watchdog_orphans import check_orphaned_threads

    assert juggle_watchdog.check_orphaned_threads is check_orphaned_threads
