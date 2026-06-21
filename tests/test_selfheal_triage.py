"""selfheal-triage-v2 P1 — pure triage logic + DB sweep/valve integration pins."""
from datetime import datetime, timezone

from juggle_db import JuggleDB
from selfheal_triage import (
    ALLOWLIST_VERSION,
    STRONG_SIGNAL_REGEX,
    classify_allowlist,
)


def test_classify_matches_sleep_timeout():
    """selfheal-v2 P1 (2026-06-21): anchored sleep-timeout transient -> rule id."""
    rid = classify_allowlist("B", "Bash", "sleep: command timed out after 120 seconds")
    assert rid is not None


def test_classify_matches_tmp_path_gone():
    """selfheal-v2 P1 (2026-06-21): tmp-path-gone is benign."""
    rid = classify_allowlist("FileNotFoundError", "juggle_worktree",
                             "no such file or directory: /tmp/juggle-xyz/foo")
    assert rid is not None


def test_classify_rejects_unanchored_substring():
    """selfheal-v2 P1 (2026-06-21): a matching regex on the WRONG entrypoint must NOT sweep."""
    # 'broken pipe' text but from a real app entrypoint with mismatched exc_type
    rid = classify_allowlist("ValueError", "juggle_graph_dispatch", "broken pipe")
    assert rid is None


def test_argparse_selfcall_never_swept():
    """selfheal-v2 P1 (2026-06-21): malformed juggle_cli self-call is a strong real-bug signal, never benign."""
    rid = classify_allowlist("B", "Bash",
                             "juggle_cli.py: error: argument command: invalid choice: 'complte-agent'")
    assert rid is None
    assert STRONG_SIGNAL_REGEX.search("error: argument command: invalid choice: 'x'")


def test_allowlist_version_is_int():
    assert isinstance(ALLOWLIST_VERSION, int) and ALLOWLIST_VERSION >= 1


def test_sweep_sets_matching_open_rows_to_non_issue(tmp_path):
    """selfheal-v2 P1 (2026-06-21): allowlist sweep hides transient open rows, leaves real ones."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    db.dedup_or_insert_error(
        "s_sleep", "B", None, "sleep: command timed out after 120 seconds", "Bash", "{}")
    db.dedup_or_insert_error(
        "s_real", "A", "ValueError", "ValueError: bad config", "juggle_graph_dispatch", "{}")
    swept = db.sweep_allowlist_to_nonissue(classify_allowlist, ALLOWLIST_VERSION)
    assert {s["signature_hash"] for s in swept} == {"s_sleep"}
    rows = {r["signature_hash"]: r["status"] for r in db.get_open_error_events(include_hidden=True)}
    assert rows["s_sleep"] == "non_issue"
    assert rows["s_real"] == "open"


def test_strong_signal_beats_high_count_noise():
    """selfheal-v2 P1 (2026-06-21): low-count argparse self-call outranks high-count noise."""
    from selfheal_triage import signal_strength, order_candidates
    noise = {"id": 1, "error_class": "B", "exc_type": None, "count": 900,
             "traceback": "git: fatal: not a repo", "command_args": "{}"}
    real = {"id": 2, "error_class": "B", "exc_type": None, "count": 4,
            "traceback": "juggle_cli.py: error: argument command: invalid choice: 'x'",
            "command_args": "{}"}
    ordered = order_candidates([noise, real])
    assert ordered[0]["id"] == 2  # strong signal first despite count 4 vs 900
    assert signal_strength(real) > signal_strength(noise)


# ---------------------------------------------------------------------------
# Task 8 — re-surface valve (surge + absolute + lease)
# ---------------------------------------------------------------------------
_NOW = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)


def test_resurface_trips_on_absolute_without_spike():
    """selfheal-v2 P1 (2026-06-21): slow-burn crosses absolute ceiling with no velocity spike."""
    from selfheal_triage import should_resurface
    row = {"count": 150, "last_seen": "2026-01-01 00:00"}  # old, never spiked
    assert should_resurface(row, _NOW, surge_count=20, absolute_count=100, lease_days=30) == "absolute"


def test_resurface_trips_on_lease_expiry():
    """selfheal-v2 P1 (2026-06-21): a still-benign group past its lease re-confirms."""
    from selfheal_triage import should_resurface
    row = {"count": 2, "last_seen": "2026-01-01 00:00"}  # >30d old, low count
    assert should_resurface(row, _NOW, surge_count=20, absolute_count=100, lease_days=30) == "lease"


def test_resurface_none_when_fresh_and_quiet():
    """selfheal-v2 P1 (2026-06-21): a recent low-count non_issue stays hidden."""
    from selfheal_triage import should_resurface
    row = {"count": 3, "last_seen": "2026-06-21 11:00"}
    assert should_resurface(row, _NOW, surge_count=20, absolute_count=100, lease_days=30) is None


def test_resurface_sweep_flips_slow_burn_to_open(tmp_path):
    """selfheal-v2 P1 (2026-06-21): a non_issue past the absolute ceiling returns to open."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    rid = db.dedup_or_insert_error("s", "B", None, "tb", "ep", "{}")
    db.set_error_event_status(rid, "non_issue")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=150 WHERE id=?", (rid,))
        conn.commit()
    out = db.resurface_nonissue_rows(_NOW, surge_count=20, absolute_count=100, lease_days=30)
    assert out and out[0]["reason"] == "absolute"
    with db._connect() as conn:
        assert conn.execute("SELECT status FROM error_events WHERE id=?", (rid,)).fetchone()[0] == "open"
