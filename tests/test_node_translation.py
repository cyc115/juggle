"""Pin (2026-06-22): legacy status->state value map is single-source for P8 read-collapse."""
import pytest


def test_status_to_state_full_map():
    from dbops.node_translation import STATUS_TO_STATE
    assert STATUS_TO_STATE == {
        "active": "open", "closed": "done", "background": "background",
        "running": "running", "failed": "failed-exec", "done": "done",
        "archived": "archived",
    }


def test_status_state_bijective_over_live_vocab():
    """2026-06-27 P8 R2-1: status<->state is bijective over the LIVE vocab; a
    'background' conversation round-trips losslessly (it was collapsed to 'running',
    which broke the watchdog reaper + the two distinct cockpit panels)."""
    from dbops.node_translation import STATUS_TO_STATE, STATE_TO_STATUS
    for status in ("active", "background", "running", "closed", "archived"):
        state = STATUS_TO_STATE[status]
        assert STATE_TO_STATUS[state] == status, f"{status!r} not invertible (state={state!r})"


def test_state_for_status_unknown_fails_loud():
    from dbops.node_translation import state_for_status
    with pytest.raises(KeyError):
        state_for_status("bogus")


def test_state_for_status_known_values():
    from dbops.node_translation import state_for_status
    assert state_for_status("active") == "open"
    assert state_for_status("background") == "background"
    assert state_for_status("failed") == "failed-exec"


def test_column_alias_constants():
    from dbops import node_translation as nt
    assert nt.TOPIC_COL == "title"
    assert nt.PROMPT_COL == "objective"
    assert nt.LAST_ACTIVE_COL == "last_active_at"
    assert nt.TOPIC_ID_COL == "parent_id"


def test_status_for_state_reverse_bijection():
    """Pin (2026-06-23, Q1): reverse-map state->status is exact for the live vocab,
    so the alias-shim reproduces threads.status faithfully after the read-collapse."""
    from dbops.node_translation import status_for_state
    assert status_for_state("open") == "active"
    assert status_for_state("running") == "running"
    assert status_for_state("done") == "closed"
    assert status_for_state("archived") == "archived"
    # legacy-only node states pass through unchanged
    assert status_for_state("failed-exec") == "failed-exec"


def test_state_as_status_sql_matches_dict():
    """2026-06-27 P8 H1: the SQL CASE must be GENERATED from STATE_TO_STATUS so the
    two encodings can never diverge — every dict entry appears as an explicit WHEN
    (the old hand-written literal mapped identity values via ELSE, so editing the
    dict silently broke the SQL) AND the SQL evaluates to the dict value in sqlite."""
    import sqlite3
    from dbops import node_translation as nt
    conn = sqlite3.connect(":memory:")
    for state, expected in nt.STATE_TO_STATUS.items():
        assert f"WHEN '{state}' THEN '{expected}'" in nt.STATE_AS_STATUS_SQL, \
            f"{state}->{expected} not explicitly encoded in generated SQL"
        got = conn.execute(
            f"SELECT {nt.STATE_AS_STATUS_SQL} FROM (SELECT ? AS state)", (state,)
        ).fetchone()[0]
        assert got == expected, f"{state}: SQL={got} dict={expected}"


def test_state_as_status_sql_round_trips_in_sqlite():
    """The CASE alias-shim must yield the legacy status when applied to a node row."""
    import sqlite3
    from dbops.node_translation import STATE_AS_STATUS_SQL
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT, state TEXT)")
    conn.executemany("INSERT INTO nodes VALUES (?,?)",
                     [("a", "open"), ("b", "running"), ("c", "done"), ("d", "archived")])
    got = dict(conn.execute(f"SELECT id, {STATE_AS_STATUS_SQL} FROM nodes").fetchall())
    assert got == {"a": "active", "b": "running", "c": "closed", "d": "archived"}
