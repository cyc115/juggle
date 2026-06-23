"""Pin (2026-06-22): legacy status->state value map is single-source for P8 read-collapse."""
import pytest


def test_status_to_state_full_map():
    from dbops.node_translation import STATUS_TO_STATE
    assert STATUS_TO_STATE == {
        "active": "open", "closed": "done", "background": "running",
        "running": "running", "failed": "failed-exec", "done": "done",
        "archived": "archived",
    }


def test_state_for_status_unknown_fails_loud():
    from dbops.node_translation import state_for_status
    with pytest.raises(KeyError):
        state_for_status("bogus")


def test_state_for_status_known_values():
    from dbops.node_translation import state_for_status
    assert state_for_status("active") == "open"
    assert state_for_status("background") == "running"
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
