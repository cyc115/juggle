"""Tests for the durable agent I/O ledger (agent_runs table + RunsMixin).

The ledger pairs each agent dispatch's INPUT (full sent prompt) with its OUTPUT
(handoff/result + diffstat), keyed by thread_id (universal) plus
project/topic/node ids so the orchestrator can answer "what was the input and
output for any project / topic / node / thread / agent".
"""

import sys
import time
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "runs.db"))
    d.init_db()
    return d


@pytest.fixture
def thread(db):
    return db.create_thread("Test topic", session_id="")


# ---------------------------------------------------------------------------
# insert_agent_run
# ---------------------------------------------------------------------------


def test_insert_agent_run_returns_id_and_persists(db, thread):
    run_id = db.insert_agent_run(
        thread_id=thread,
        input_prompt="FULL PROMPT BYTES",
        agent_id="agent-1",
        role="coder",
        model="opus",
        harness="claude",
        project_id="P1",
        topic_id="T1",
        node_id="N1",
    )
    assert isinstance(run_id, int)
    row = db.get_run(run_id)
    assert row is not None
    assert row["thread_id"] == thread
    assert row["input_prompt"] == "FULL PROMPT BYTES"
    assert row["agent_id"] == "agent-1"
    assert row["role"] == "coder"
    assert row["model"] == "opus"
    assert row["harness"] == "claude"
    assert row["project_id"] == "P1"
    assert row["topic_id"] == "T1"
    assert row["node_id"] == "N1"
    assert row["status"] == "dispatched"
    assert row["dispatched_at"]
    assert row["completed_at"] is None
    assert row["output"] is None
    assert row["diffstat"] is None


# ---------------------------------------------------------------------------
# close_run
# ---------------------------------------------------------------------------


def test_close_run_updates_newest_open_only(db, thread):
    old_id = db.insert_agent_run(
        thread_id=thread, input_prompt="old", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    time.sleep(0.01)
    new_id = db.insert_agent_run(
        thread_id=thread, input_prompt="new", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.close_run(thread, output="DONE", diffstat="1 file changed")

    old = db.get_run(old_id)
    new = db.get_run(new_id)
    # newest open run got closed
    assert new["status"] == "completed"
    assert new["output"] == "DONE"
    assert new["diffstat"] == "1 file changed"
    assert new["completed_at"]
    # older one is untouched (still open)
    assert old["status"] == "dispatched"
    assert old["output"] is None


def test_close_run_sets_status_arg(db, thread):
    rid = db.insert_agent_run(
        thread_id=thread, input_prompt="x", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.close_run(thread, output="boom", diffstat=None, status="failed")
    assert db.get_run(rid)["status"] == "failed"


def test_close_run_noop_when_no_open_run(db, thread):
    # Should not raise even if there is no open run.
    db.close_run(thread, output="x", diffstat=None)


# ---------------------------------------------------------------------------
# supersede_open_runs
# ---------------------------------------------------------------------------


def test_supersede_open_runs_flips_open(db, thread):
    rid = db.insert_agent_run(
        thread_id=thread, input_prompt="first", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.supersede_open_runs(thread)
    assert db.get_run(rid)["status"] == "superseded"


def test_second_dispatch_supersedes_first(db, thread):
    rid1 = db.insert_agent_run(
        thread_id=thread, input_prompt="first", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.supersede_open_runs(thread)
    rid2 = db.insert_agent_run(
        thread_id=thread, input_prompt="second", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    assert db.get_run(rid1)["status"] == "superseded"
    assert db.get_run(rid2)["status"] == "dispatched"


# ---------------------------------------------------------------------------
# fail_open_runs
# ---------------------------------------------------------------------------


def test_fail_open_runs_by_thread(db, thread):
    rid = db.insert_agent_run(
        thread_id=thread, input_prompt="x", agent_id="agent-9", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.fail_open_runs(thread_id=thread)
    assert db.get_run(rid)["status"] == "failed"


def test_fail_open_runs_by_agent(db, thread):
    rid = db.insert_agent_run(
        thread_id=thread, input_prompt="x", agent_id="agent-9", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.fail_open_runs(agent_id="agent-9")
    assert db.get_run(rid)["status"] == "failed"


def test_fail_open_runs_leaves_closed_alone(db, thread):
    rid = db.insert_agent_run(
        thread_id=thread, input_prompt="x", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    db.close_run(thread, output="done", diffstat=None)
    db.fail_open_runs(thread_id=thread)
    assert db.get_run(rid)["status"] == "completed"


# ---------------------------------------------------------------------------
# get_runs filtering / ordering / limit
# ---------------------------------------------------------------------------


def _seed(db, thread, **kw):
    base = dict(
        thread_id=thread, input_prompt="p", agent_id="a", role="coder",
        model=None, harness=None, project_id=None, topic_id=None, node_id=None,
    )
    base.update(kw)
    return db.insert_agent_run(**base)


def test_get_runs_filter_by_each_key(db):
    t1 = db.create_thread("t1", session_id="")
    t2 = db.create_thread("t2", session_id="")
    r_proj = _seed(db, t1, project_id="PA")
    r_topic = _seed(db, t1, topic_id="TB")
    r_node = _seed(db, t1, node_id="NC")
    r_thread2 = _seed(db, t2, project_id="PZ")

    assert {r["id"] for r in db.get_runs(project_id="PA")} == {r_proj}
    assert {r["id"] for r in db.get_runs(topic_id="TB")} == {r_topic}
    assert {r["id"] for r in db.get_runs(node_id="NC")} == {r_node}
    assert {r["id"] for r in db.get_runs(thread_id=t2)} == {r_thread2}


def test_get_runs_ordering_newest_first_and_limit(db, thread):
    ids = []
    for i in range(3):
        ids.append(_seed(db, thread, input_prompt=f"p{i}"))
        time.sleep(0.01)
    got = db.get_runs(thread_id=thread)
    assert [r["id"] for r in got] == list(reversed(ids))
    limited = db.get_runs(thread_id=thread, limit=1)
    assert len(limited) == 1
    assert limited[0]["id"] == ids[-1]


def test_get_run_by_id_missing_returns_none(db):
    assert db.get_run(999999) is None


# ---------------------------------------------------------------------------
# prune_runs
# ---------------------------------------------------------------------------


def test_prune_runs_deletes_only_older_than_cutoff(db, thread):
    import datetime as _dt

    fresh = _seed(db, thread, input_prompt="fresh")
    stale = _seed(db, thread, input_prompt="stale")
    # Backdate the stale row 100 days.
    old_ts = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=100)
    ).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE agent_runs SET dispatched_at=? WHERE id=?", (old_ts, stale)
        )
        conn.commit()

    deleted = db.prune_runs(older_than_days=90)
    assert deleted == 1
    assert db.get_run(stale) is None
    assert db.get_run(fresh) is not None


# ---------------------------------------------------------------------------
# Migration idempotency
# ---------------------------------------------------------------------------


def test_migration_idempotent(tmp_path):
    p = str(tmp_path / "mig.db")
    d = JuggleDB(p)
    d.init_db()
    d.init_db()  # second run must not raise
    # table exists and is usable
    t = d.create_thread("x", session_id="")
    rid = d.insert_agent_run(
        thread_id=t, input_prompt="x", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    assert d.get_run(rid) is not None


def test_existing_db_without_table_gets_it(tmp_path):
    """A DB created before the agent_runs migration gets the table on re-open."""
    import sqlite3

    p = str(tmp_path / "legacy.db")
    d = JuggleDB(p)
    d.init_db()
    # Simulate a pre-migration DB by dropping the table + the agents column.
    with sqlite3.connect(p) as conn:
        conn.execute("DROP TABLE IF EXISTS agent_runs")
        conn.commit()
    # Re-open and migrate.
    d2 = JuggleDB(p)
    d2.init_db()
    t = d2.create_thread("x", session_id="")
    rid = d2.insert_agent_run(
        thread_id=t, input_prompt="x", agent_id="a", role="coder",
        model=None, harness=None, project_id="P", topic_id=None, node_id=None,
    )
    assert d2.get_run(rid) is not None


def test_agents_table_has_current_run_id_column(db):
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
    assert "current_run_id" in cols
