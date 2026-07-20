"""Migration 58 (spool_journal), should_spool() gate, open_connection_readonly,
and resolve_thread_id_for_spool — the Task-2 prerequisites Tasks 3-6 build on."""
import sqlite3

import pytest

import juggle_cli_common as cli_common
from juggle_db import JuggleDB
from juggle_db_connect import open_connection_readonly


def test_init_db_creates_spool_journal_table(tmp_path):
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(spool_journal)").fetchall()}
    assert cols == {"uuid", "event_type", "applied_at", "outcome"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    # Neutralize the cwd-based worktree signal (is_agent_context() also matches
    # "juggle-juggle-" in cwd) so this test is deterministic regardless of
    # where pytest is invoked from — see test_autopilot_guards.py precedent.
    monkeypatch.chdir(tmp_path)


def test_should_spool_false_by_default():
    assert cli_common.should_spool() is False


def test_should_spool_true_when_juggle_is_agent(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    assert cli_common.should_spool() is True


def test_should_spool_false_when_orchestrator_marker_set(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    assert cli_common.should_spool() is False


def test_readonly_connection_refuses_insert(tmp_path):
    db_path = tmp_path / "j.db"
    db = JuggleDB(str(db_path))
    db.init_db()

    ro_conn = open_connection_readonly(db_path)
    with pytest.raises(sqlite3.OperationalError):
        ro_conn.execute("INSERT INTO session(key, value) VALUES ('x','1')")
        ro_conn.commit()


def test_readonly_connection_allows_select(tmp_path):
    db_path = tmp_path / "j.db"
    db = JuggleDB(str(db_path))
    db.init_db()

    ro_conn = open_connection_readonly(db_path)
    rows = ro_conn.execute("SELECT * FROM nodes WHERE kind='conversation'").fetchall()
    assert rows == []


def test_resolve_thread_id_for_spool_resolves_label_to_uuid(tmp_path, monkeypatch):
    db_path = tmp_path / "j.db"
    db = JuggleDB(str(db_path))
    db.init_db()
    tid = db.create_thread("test topic", "t-abc")
    label = db.get_thread(tid)["user_label"]
    monkeypatch.setattr("juggle_cli_common.DB_PATH", db_path, raising=False)
    monkeypatch.setattr("juggle_cli_common._db_path", lambda: db_path, raising=False)

    resolved = cli_common.resolve_thread_id_for_spool(label)
    assert resolved == tid


def test_resolve_thread_id_for_spool_passes_through_uuid_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("juggle_cli_common._db_path", lambda: tmp_path / "j.db", raising=False)
    full_uuid = "12345678-1234-1234-1234-123456789012"
    assert cli_common.resolve_thread_id_for_spool(full_uuid) == full_uuid


def test_resolve_thread_id_for_spool_best_effort_on_missing_db(tmp_path, monkeypatch):
    """DB doesn't exist yet — resolution degrades to passthrough, never raises."""
    monkeypatch.setattr("juggle_cli_common._db_path", lambda: tmp_path / "does-not-exist.db",
                         raising=False)
    assert cli_common.resolve_thread_id_for_spool("ZZ") == "ZZ"


def test_resolve_thread_id_for_spool_newest_wins_under_label_collision(tmp_path, monkeypatch):
    """Bug#1 attempt#5: a recycled label (T-slug-wheel) can be shared by many
    kind='conversation' nodes. resolve_thread_id_for_spool's own raw SELECT had
    no ORDER BY (unlike the canonical db.get_thread_by_user_label chokepoint,
    which is newest-live-wins) — so under collision it could resolve `agent
    complete <label>` to a STALE archived node instead of the current live
    adhoc coder thread, silently skipping that thread's ensure_adhoc_topic_wrapper
    (the thread it should have resolved to is never wrapped/merged at all)."""
    db_path = tmp_path / "j.db"
    db = JuggleDB(str(db_path))
    db.init_db()

    stale = db.create_thread("Old holder", session_id="s1")
    db.archive_thread(stale)  # keeps its label, but terminal — not live

    live = db.create_thread("New holder", session_id="s1")
    db.update_thread(live, worktree_path="/tmp/wt-live", worktree_branch="cyc_LA",
                      main_repo_path="/tmp/repo-live")
    with db._connect() as conn:
        # Force the label collision onto the authoritative conversation node
        # (mirrors test_get_thread_by_user_label_newest_wins_after_reuse).
        conn.execute("UPDATE nodes SET user_label = 'LA' WHERE id = ? "
                     "AND kind='conversation'", (stale,))
        conn.execute("UPDATE nodes SET user_label = 'LA' WHERE id = ? "
                     "AND kind='conversation'", (live,))
        conn.commit()

    monkeypatch.setattr("juggle_cli_common._db_path", lambda: db_path, raising=False)

    resolved = cli_common.resolve_thread_id_for_spool("LA")
    assert resolved == live, (
        f"expected live worktree-bound holder {live[:8]}, got {resolved[:8]} — "
        "a stale label-collision holder was resolved instead"
    )
