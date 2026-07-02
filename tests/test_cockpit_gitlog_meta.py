"""DB-aware bulk metadata fetch tests for the git-log full-screen view."""
from datetime import datetime, timezone

from juggle_cockpit_gitlog_meta import gitlog_meta_for, GitLogMeta


def _now():
    return datetime.now(timezone.utc).isoformat()


def _insert_topic(db, tid, project_id="P", state="verified", merged_sha=None, verified_at=None):
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", (project_id, project_id, "active", now, now),
        )
        conn.execute(
            "INSERT INTO nodes(id,kind,title,objective,state,project_id,merged_sha,"
            "verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tid, "topic", tid, "obj", state, project_id, merged_sha, verified_at, now, now),
        )
        conn.commit()


def _insert_run(db, *, topic_id, role, model, dispatched_at, completed_at=None, status="completed"):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO agent_runs(thread_id,project_id,topic_id,role,model,"
            "input_prompt,status,dispatched_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (f"t-{topic_id}", "P", topic_id, role, model, "p", status, dispatched_at, completed_at),
        )
        conn.commit()


def test_empty_ids_returns_empty_dict(juggle_db):
    assert gitlog_meta_for(juggle_db, []) == {}


def test_missing_id_absent_from_result(juggle_db):
    assert gitlog_meta_for(juggle_db, ["nope"]) == {"nope": GitLogMeta()}


def test_merged_sha_and_verified_at_come_from_nodes(juggle_db):
    _insert_topic(juggle_db, "a", merged_sha="6db0e75", verified_at="2026-07-02T09:12:00+00:00")
    meta = gitlog_meta_for(juggle_db, ["a"])
    assert meta["a"].merged_sha == "6db0e75"
    assert meta["a"].verified_at == "2026-07-02T09:12:00+00:00"


def test_latest_run_picked_by_highest_id(juggle_db):
    """Two runs for the same topic — the NEWEST (higher id) wins."""
    _insert_topic(juggle_db, "a")
    _insert_run(juggle_db, topic_id="a", role="coder", model="haiku",
                dispatched_at="2026-07-02T08:00:00+00:00", completed_at="2026-07-02T08:05:00+00:00")
    _insert_run(juggle_db, topic_id="a", role="coder", model="sonnet",
                dispatched_at="2026-07-02T09:00:00+00:00", completed_at="2026-07-02T09:12:00+00:00")
    meta = gitlog_meta_for(juggle_db, ["a"])
    assert meta["a"].model == "sonnet"
    assert meta["a"].dispatched_at == "2026-07-02T09:00:00+00:00"


class _CountingConn:
    """Wraps a real connection, counting SELECTs so N ids still cost O(1) queries."""

    def __init__(self, conn):
        self._conn = conn
        self.calls = []

    def execute(self, sql, *a, **kw):
        if sql.strip().upper().startswith("SELECT"):
            self.calls.append(sql)
        return self._conn.execute(sql, *a, **kw)

    def close(self):
        self._conn.close()


def test_one_query_per_table_no_n_plus_1(juggle_db, monkeypatch):
    """Bulk fetch: exactly 2 SELECTs regardless of id count (nodes + agent_runs)."""
    for tid in ("a", "b", "c"):
        _insert_topic(juggle_db, tid)
    orig_connect = juggle_db._connect
    wrapper = _CountingConn(orig_connect())
    monkeypatch.setattr(juggle_db, "_connect", lambda: wrapper)
    gitlog_meta_for(juggle_db, ["a", "b", "c"])
    assert len(wrapper.calls) == 2
