"""Tests for the graph-mirrors-threads feature (Option 1, 2026-06-14).

All tests use isolated tmp_path DBs — never the shared prod DB.

Pins:
- Migration 42 idempotent + additive
- db_mirror: upsert, delete, backfill, reconcile
- guard bypass for is_mirror=1 topics
- topic_ready_eligible never returns is_mirror=1
- topic_counts counts is_mirror=0 only
- progress tally: N mirror nodes don't inflate the count
- Regression: armed-project with mirror nodes still allows manual send-task
  to a NON-mirror topic AND doesn't refuse on mirror nodes
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(db, pid="P1") -> str:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, f"Project {pid}", "active", _now(), _now()),
        )
        conn.commit()
    return pid


def _thread(db, project_id=None, status="active", topic="do work") -> str:
    tid = db.create_thread(topic=topic, session_id="sessA")
    updates = {"status": status}
    if project_id:
        updates["project_id"] = project_id
        updates["assigned_by"] = "human"
    db.update_thread(tid, **updates)
    return tid


# ---------------------------------------------------------------------------
# Migration 42: is_mirror column
# ---------------------------------------------------------------------------

class TestMigration42:
    def test_column_exists_after_init(self, db):
        """is_mirror column exists on graph_topics after init_db."""
        with db._connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(graph_topics)").fetchall()}
        assert "is_mirror" in cols

    def test_default_is_zero(self, db):
        """Existing graph_topics rows default to is_mirror=0."""
        from dbops.db_topics import create_topic
        _project(db)
        create_topic(db, topic_id="T1", project_id="P1", title="Real work")
        with db._connect() as conn:
            row = conn.execute(
                "SELECT is_mirror FROM graph_topics WHERE id='T1'"
            ).fetchone()
        assert row["is_mirror"] == 0

    def test_migration_idempotent(self, db):
        """Re-running migration 42 is a no-op (no duplicate column error)."""
        from dbops.migrations_graph import migrate_is_mirror
        with db._connect() as conn:
            # Should not raise
            migrate_is_mirror(conn)
            migrate_is_mirror(conn)

    def test_existing_rows_get_default(self, db):
        """Rows inserted BEFORE migration still get is_mirror=0 (SQLite default)."""
        with db._connect() as conn:
            # Insert directly bypassing ORM to simulate pre-migration row
            conn.execute(
                "INSERT INTO graph_topics(id,project_id,title,objective,state,created_at,updated_at) "
                "VALUES('T-old','P1','Old','','open',?,?)",
                (_now(), _now()),
            )
            conn.commit()
        with db._connect() as conn:
            row = conn.execute(
                "SELECT is_mirror FROM graph_topics WHERE id='T-old'"
            ).fetchone()
        assert row["is_mirror"] == 0


# ---------------------------------------------------------------------------
# db_mirror: mirror_upsert_thread
# ---------------------------------------------------------------------------

class TestMirrorUpsert:
    def test_creates_mirror_topic(self, db):
        """mirror_upsert_thread creates an is_mirror=1 topic for the thread."""
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        mirror_id = mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute(
                "SELECT * FROM graph_topics WHERE id=?", (mirror_id,)
            ).fetchone()
        assert row is not None
        assert row["is_mirror"] == 1
        assert row["project_id"] == "P1"
        assert row["thread_id"] == tid

    def test_upsert_is_idempotent(self, db):
        """Calling mirror_upsert_thread twice doesn't create two rows."""
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        mirror_upsert_thread(db, tid, "P1")
        mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid,)
            ).fetchone()[0]
        assert count == 1

    def test_active_thread_maps_to_running(self, db):
        """Thread status 'active' → mirror topic state 'running'."""
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        mirror_id = mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "running"

    def test_idle_thread_maps_to_pending(self, db):
        """Thread status 'idle' → mirror topic state 'open'."""
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="idle")
        mirror_id = mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "open"

    def test_done_thread_maps_to_verified(self, db):
        """Thread status 'done' → mirror topic state 'verified'."""
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="done")
        mirror_id = mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "verified"

    def test_project_reassign_leaves_exactly_one_mirror(self, db):
        """Re-assigning a thread to a different project: delete-before-insert
        leaves exactly one mirror node total (no orphan in old project).

        Incident context: 2026-06-14 graph-mirrors spec DA resolution.
        """
        from dbops.db_mirror import mirror_upsert_thread

        _project(db, "P1")
        _project(db, "P2")
        tid = _thread(db, project_id="P1", status="active")
        mirror_upsert_thread(db, tid, "P1")
        # Reassign to P2
        mirror_upsert_thread(db, tid, "P2")

        with db._connect() as conn:
            rows = conn.execute(
                "SELECT project_id FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid,)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["project_id"] == "P2"


# ---------------------------------------------------------------------------
# db_mirror: mirror_delete_thread
# ---------------------------------------------------------------------------

class TestMirrorDelete:
    def test_delete_removes_mirror(self, db):
        """mirror_delete_thread removes the is_mirror=1 topic for the thread."""
        from dbops.db_mirror import mirror_upsert_thread, mirror_delete_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        mirror_upsert_thread(db, tid, "P1")
        mirror_delete_thread(db, tid)

        with db._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid,)
            ).fetchone()[0]
        assert count == 0

    def test_delete_noop_when_no_mirror(self, db):
        """mirror_delete_thread is safe when no mirror exists for the thread."""
        from dbops.db_mirror import mirror_delete_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        # No mirror created — should not raise
        mirror_delete_thread(db, tid)

    def test_delete_leaves_real_topics_intact(self, db):
        """mirror_delete_thread does NOT remove real (is_mirror=0) topics."""
        from dbops.db_mirror import mirror_upsert_thread, mirror_delete_thread
        from dbops.db_topics import create_topic

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        create_topic(db, topic_id="T-real", project_id="P1", title="Real work")
        mirror_upsert_thread(db, tid, "P1")
        mirror_delete_thread(db, tid)

        with db._connect() as conn:
            row = conn.execute("SELECT * FROM graph_topics WHERE id='T-real'").fetchone()
        assert row is not None


# ---------------------------------------------------------------------------
# db_mirror: backfill_mirror_topics
# ---------------------------------------------------------------------------

class TestBackfill:
    def test_backfill_creates_mirrors_for_assigned_threads(self, db):
        """backfill_mirror_topics creates mirror topics for project-assigned threads."""
        from dbops.db_mirror import backfill_mirror_topics

        _project(db)
        # Distinct topics: the create_thread dedup guard collapses
        # lexically-identical open threads, so two backfill targets need
        # genuinely different titles.
        tid1 = _thread(db, project_id="P1", status="active", topic="alpha work")
        tid2 = _thread(db, project_id="P1", status="idle", topic="beta work")

        count = backfill_mirror_topics(db)
        assert count >= 2

        with db._connect() as conn:
            mirrors = conn.execute(
                "SELECT thread_id FROM graph_topics WHERE is_mirror=1 AND project_id='P1'"
            ).fetchall()
        mirror_thread_ids = {r["thread_id"] for r in mirrors}
        assert tid1 in mirror_thread_ids
        assert tid2 in mirror_thread_ids

    def test_backfill_skips_inbox_threads(self, db):
        """backfill_mirror_topics does not create mirrors for INBOX threads."""
        from dbops.db_mirror import backfill_mirror_topics

        _project(db)
        tid_inbox = _thread(db, project_id="INBOX", status="active")
        backfill_mirror_topics(db)

        with db._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid_inbox,)
            ).fetchone()[0]
        assert count == 0

    def test_backfill_skips_archived_threads(self, db):
        """backfill_mirror_topics does not create mirrors for archived threads."""
        from dbops.db_mirror import backfill_mirror_topics

        _project(db)
        tid = _thread(db, project_id="P1", status="archived")
        backfill_mirror_topics(db)

        with db._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid,)
            ).fetchone()[0]
        assert count == 0

    def test_backfill_is_idempotent(self, db):
        """backfill_mirror_topics run twice leaves exactly one mirror per thread."""
        from dbops.db_mirror import backfill_mirror_topics

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        backfill_mirror_topics(db)
        backfill_mirror_topics(db)

        with db._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM graph_topics WHERE thread_id=? AND is_mirror=1",
                (tid,)
            ).fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# topic_ready_eligible excludes is_mirror=1
# ---------------------------------------------------------------------------

class TestReadyEligibleExcludesMirror:
    def test_mirror_topic_never_eligible(self, db):
        """topic_ready_eligible does not return is_mirror=1 topics.

        Regression pin for 2026-06-14: mirror topics are trackers and must
        never be dispatched by the watchdog tick.
        """
        from dbops.db_mirror import mirror_upsert_thread
        from dbops.db_topics import topic_ready_eligible

        _project(db)
        tid = _thread(db, project_id="P1", status="active")
        mirror_upsert_thread(db, tid, "P1")

        # Force mirror to pending (eligible state for normal topics)
        mirror_id = f"~{tid}"
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state='open' WHERE id=? AND is_mirror=1",
                (mirror_id,)
            )
            conn.commit()

        eligible = topic_ready_eligible(db, "P1")
        assert mirror_id not in eligible


# ---------------------------------------------------------------------------
# topic_counts excludes is_mirror=1
# ---------------------------------------------------------------------------

class TestTopicCountsExcludesMirror:
    def test_mirror_topics_not_counted(self, db):
        """topic_counts counts only is_mirror=0 topics.

        Regression pin for 2026-06-14: P2's 14/14 tally must not inflate
        when mirror nodes are present.
        """
        from dbops.db_mirror import mirror_upsert_thread
        from dbops.db_topics import create_topic, topic_counts

        _project(db)
        # Create 2 real topics in verified state
        create_topic(db, topic_id="T1", project_id="P1", title="Task 1")
        create_topic(db, topic_id="T2", project_id="P1", title="Task 2")
        with db._connect() as conn:
            conn.execute("UPDATE graph_topics SET state='verified' WHERE id IN ('T1','T2')")
            conn.commit()

        # Create 5 mirror topics (should NOT affect count)
        for i in range(5):
            tid = _thread(db, project_id="P1", status="done")
            mirror_upsert_thread(db, tid, "P1")

        counts = topic_counts(db, "P1")
        assert counts is not None
        assert counts["total"] == 2, f"expected 2, got {counts['total']}"
        assert counts["verified"] == 2


# ---------------------------------------------------------------------------
# check_task_guard bypass for is_mirror=1
# ---------------------------------------------------------------------------

class TestGuardBypassForMirror:
    def test_guard_refuses_real_topic_in_tick_owned_state(self, db):
        """check_task_guard refuses send-task for a real topic in tick-owned state.

        Regression pin: baseline behavior must be preserved.
        """
        from dbops.db_topics import create_topic, set_topic_thread
        from juggle_cmd_agents_graph import check_task_guard

        _project(db, "P1")
        tid = _thread(db, project_id="P1", status="active")
        create_topic(db, topic_id="T-real", project_id="P1", title="Real")
        set_topic_thread(db, "T-real", tid)
        with db._connect() as conn:
            conn.execute("UPDATE graph_topics SET state='ready' WHERE id='T-real'")
            conn.commit()

        err = check_task_guard(db, tid)
        assert err is not None, "expected a guard error for a real tick-owned topic"

    def test_guard_allows_mirror_topic(self, db):
        """check_task_guard bypasses (returns None) for is_mirror=1 topics.

        Regression pin for 2026-06-14: mirror tracker nodes must never cause
        a manual-send-task refusal.
        """
        from dbops.db_mirror import mirror_upsert_thread
        from juggle_cmd_agents_graph import check_task_guard

        _project(db, "P1")
        tid = _thread(db, project_id="P1", status="active")
        mirror_id = mirror_upsert_thread(db, tid, "P1")
        # Drive mirror to tick-owned state directly
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state='ready', thread_id=? WHERE id=?",
                (tid, mirror_id)
            )
            conn.commit()

        err = check_task_guard(db, tid)
        assert err is None, f"expected None for mirror topic, got: {err!r}"

    def test_guard_allows_send_to_real_topic_despite_mirror(self, db):
        """Manual send-task to a NON-mirror topic is still allowed even when
        the project has mirror nodes.

        Regression pin for 2026-06-14: the mirror bypass must not leak into
        real topic guard logic — armed-project with mirrors still dispatches.
        """
        from dbops.db_mirror import mirror_upsert_thread
        from dbops.db_topics import create_topic, set_topic_thread
        from juggle_cmd_agents_graph import check_task_guard

        _project(db, "P1")

        # A non-mirror thread with a real pending topic (not tick-owned)
        tid_real = _thread(db, project_id="P1", status="active", topic="real work")
        create_topic(db, topic_id="T-real", project_id="P1", title="Real task")
        set_topic_thread(db, "T-real", tid_real)
        # Leave in pending — operator territory (not tick-owned), so guard returns None
        with db._connect() as conn:
            conn.execute("UPDATE graph_topics SET state='open' WHERE id='T-real'")
            conn.commit()

        # Some mirror topics for other threads in the same project
        for i in range(3):
            mt = _thread(db, project_id="P1", status="active", topic=f"mirror {i}")
            mirror_upsert_thread(db, mt, "P1")

        err = check_task_guard(db, tid_real)
        assert err is None, f"expected None for real pending topic, got: {err!r}"


# ---------------------------------------------------------------------------
# Cockpit: mirror cells excluded from progress count
# ---------------------------------------------------------------------------

class TestCockpitProgressCount:
    def test_progress_bar_excludes_mirrors(self, db):
        """build_graph_panel progress count excludes is_mirror=1 tasks.

        14 real verified + N mirror nodes → '14/14 done' not '14/(14+N) done'.
        """
        from juggle_cockpit_graph_layout import GraphTask
        from juggle_cockpit_graph_panel import build_graph_panel
        import io
        from rich.console import Console

        real_tasks = [
            GraphTask(id=f"T{i}", title=f"Task {i}", state="verified")
            for i in range(14)
        ]
        mirror_tasks = [
            GraphTask(id=f"~M{i}", title=f"mirror {i}", state="running", is_mirror=True)
            for i in range(5)
        ]
        all_tasks = real_tasks + mirror_tasks

        panel = build_graph_panel(
            project_id="P2", tasks=all_tasks, edges=[],
            selection=0, unread=0, width=120, height=30, pan_offset=0,
        )

        buf = io.StringIO()
        Console(width=120, file=buf, no_color=True, highlight=False).print(panel)
        out = buf.getvalue()

        assert "14/14" in out, f"expected 14/14 in output, got: {out[:300]}"
        assert "19" not in out or "14/14" in out  # total should be 14, not 19


# ---------------------------------------------------------------------------
# Defense-in-depth: mirror claim/promote/dispatch guards
# Regression pin: 2026-06-15 — stale watchdog code promoted/claimed a mirror
# node → it reached failed-verify. Single eligibility-filter defense not enough.
# ---------------------------------------------------------------------------

def _mirror_pending_with_task(db, project_id="P1"):
    """Create an is_mirror=1 topic in 'open' state with one dispatchable task.

    This simulates a mirror that WOULD be eligible for promotion if not for the
    is_mirror flag — used to prove the SQL-level guards reject it independently.
    """
    from dbops.db_mirror import mirror_upsert_thread
    from dbops.db_graph import create_task

    tid = _thread(db, project_id=project_id, status="idle")  # idle → pending mirror
    mirror_id = mirror_upsert_thread(db, tid, project_id)

    # Force mirror to 'open' (idle maps to pending already, but be explicit)
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_topics SET state='open' WHERE id=? AND is_mirror=1",
            (mirror_id,)
        )
        conn.commit()

    # Give it a dispatchable task so it would pass the "has task" eligibility gate
    task_id = f"task-{mirror_id}"
    create_task(db, task_id=task_id, project_id=project_id,
                title="phantom task", prompt="do nothing")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET topic_id=? WHERE id=?", (mirror_id, task_id)
        )
        conn.commit()

    return mirror_id, tid


class TestMirrorClaimGuard:
    """Defense-in-depth: mirror nodes must be refused by EVERY execution-transition
    SQL, independently of the eligibility filter.

    2026-06-15 incident: stale watchdog (pre-filter code) promoted/claimed a mirror
    node → it reached failed-verify state.
    """

    def test_recompute_cas_update_refuses_mirror(self, db, monkeypatch):
        """recompute_topic_ready's CAS UPDATE refuses is_mirror=1 even when
        topic_ready_eligible (the first-line filter) is bypassed.

        Simulates the stale-code scenario: eligibility filter omitted (monkeypatched
        to return the mirror id directly) → CAS UPDATE is the last-resort guard.

        2026-06-15 regression pin: single-filter defense insufficient.
        """
        import dbops.db_topics as dt

        _project(db)
        mirror_id, _ = _mirror_pending_with_task(db)

        # Bypass the eligibility filter: pretend it returned the mirror id
        monkeypatch.setattr(dt, "topic_ready_eligible", lambda db_, pid: [mirror_id])

        promoted = dt.recompute_topic_ready(db, "P1")

        assert mirror_id not in promoted, "mirror must NOT be promoted even when eligibility filter bypassed"

        with db._connect() as conn:
            row = conn.execute(
                "SELECT state FROM graph_topics WHERE id=?", (mirror_id,)
            ).fetchone()
        assert row["state"] == "open", "mirror must remain 'open'"

    def test_recompute_topic_ready_promotes_real_not_mirror(self, db):
        """recompute_topic_ready promotes real pending topics but NOT mirror topics.

        2026-06-15 regression pin: mirror topics must stay 'open' regardless of
        the eligibility filter's is_mirror guard.
        """
        from dbops.db_topics import create_topic, recompute_topic_ready
        from dbops.db_graph import create_task

        _project(db)
        mirror_id, _ = _mirror_pending_with_task(db)

        # Also create a real pending topic with a task (should be promoted)
        create_topic(db, topic_id="T-real", project_id="P1", title="Real task")
        create_task(db, task_id="task-real", project_id="P1",
                    title="real task", prompt="do real work")
        with db._connect() as conn:
            conn.execute("UPDATE graph_tasks SET topic_id='T-real' WHERE id='task-real'")
            conn.commit()

        promoted = recompute_topic_ready(db, "P1")

        assert "T-real" in promoted, "real pending topic must be promoted"
        assert mirror_id not in promoted, "mirror topic must NOT be promoted"

        with db._connect() as conn:
            mirror_row = conn.execute(
                "SELECT state FROM graph_topics WHERE id=?", (mirror_id,)
            ).fetchone()
        assert mirror_row["state"] == "open", "mirror state must remain 'open'"

    def test_claim_topic_refuses_mirror(self, db):
        """claim_topic returns False (rowcount 0) for a mirror topic.

        Even if a mirror somehow reached 'ready', the atomic claim SQL must refuse it.
        """
        import juggle_graph_dispatch as gd

        _project(db)
        mirror_id, _ = _mirror_pending_with_task(db)

        # Force mirror to 'ready' to make it look claimable
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state='ready' WHERE id=? AND is_mirror=1",
                (mirror_id,)
            )
            conn.commit()

        result = gd.claim_topic(db, mirror_id)

        assert result is False, "claim_topic must refuse is_mirror=1 topic (return False)"

        with db._connect() as conn:
            row = conn.execute(
                "SELECT state FROM graph_topics WHERE id=?", (mirror_id,)
            ).fetchone()
        assert row["state"] == "ready", "mirror state must remain 'ready' (not promoted to dispatching)"

    def test_topic_transition_refuses_mirror(self, db):
        """topic_transition raises ValueError for is_mirror=1 topics.

        Covers all other execution transitions (dispatch, running, integrating, verify).
        """
        from dbops.db_topics import topic_transition

        _project(db)
        mirror_id, _ = _mirror_pending_with_task(db)

        with pytest.raises(ValueError, match="mirror"):
            topic_transition(db, mirror_id, "deps_ready")

        with db._connect() as conn:
            row = conn.execute(
                "SELECT state FROM graph_topics WHERE id=?", (mirror_id,)
            ).fetchone()
        assert row["state"] == "open", "mirror state must not change via topic_transition"

    def test_mirror_reflection_writes_still_work(self, db):
        """mirror_upsert_thread can still update a mirror node's state (reflection).

        Regression: the claim guards must NOT block legitimate mirror reflection
        writes from db_mirror — those use direct SQL, not topic_transition.
        """
        from dbops.db_mirror import mirror_upsert_thread

        _project(db)
        tid = _thread(db, project_id="P1", status="idle")  # idle → pending
        mirror_id = mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "open"

        # Thread goes active → mirror should reflect 'running'
        db.update_thread(tid, status="active")
        mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "running", "mirror reflection must update state to 'running'"

        # Thread done → mirror should reflect 'verified'
        db.update_thread(tid, status="done")
        mirror_upsert_thread(db, tid, "P1")

        with db._connect() as conn:
            row = conn.execute("SELECT state FROM graph_topics WHERE id=?", (mirror_id,)).fetchone()
        assert row["state"] == "verified", "mirror reflection must update state to 'verified'"
