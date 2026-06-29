"""P8 T-c3-reads — project-keyed conversation read-flip pins (2026-06-29).

Unblocks the ratchet floor's reason (b): "project-keyed reads blocked by the
nodes.project_id mirror gap (create_thread's DEFAULT 'INBOX' is never mirrored)".

The preparatory refactor populates nodes.project_id (mirror_conv_insert + the
Migration-50 backfill); then the project-keyed conversation READ consumers
(synth_project, check_and_resynth_if_drifted, resweep_inbox, get_threads_by_project)
flip from `threads` to `nodes WHERE kind='conversation'`, adopting state/title.

Each read pin diverges the node from its legacy threads row and asserts the read
resolves the NODE value — RED pre-flip (read hits threads), GREEN post-flip.
"""
from __future__ import annotations

from unittest.mock import patch

from juggle_db import JuggleDB
from helpers.node_seed import seed_node

_T = "2026-01-01 00:00"


def _fresh(tmp_path):
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    # P8 terminal: legacy threads dropped on init_db; re-create it so the
    # Migration-50 backfill test can seed a pre-mirror prod threads row.
    from helpers.node_seed import make_legacy_tables
    with db._connect() as conn:
        make_legacy_tables(conn, "threads")
    return db


# ── preparatory: nodes.project_id population ──────────────────────────────────

def test_create_thread_mirrors_project_id_inbox(tmp_path):
    """create_thread's conversation node inherits the threads DEFAULT
    project_id='INBOX'. Pre-fix mirror_conv_insert omitted project_id, so the
    node's project_id was NULL and resweep_inbox over nodes found nothing."""
    db = _fresh(tmp_path)
    tid = db.create_thread("topic", session_id="s1")
    with db._connect() as conn:
        row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (tid,)).fetchone()
    assert row[0] == "INBOX"


def test_backfill_populates_node_project_id(tmp_path):
    """Migration-50 backfill copies threads.project_id onto id-matched conversation
    nodes (prod DBs created before the mirror fix had NULL node.project_id). P8
    c4-write-cut: create_thread/update_thread no longer write threads, so seed the
    legacy threads row DIRECTLY to simulate that pre-mirror prod DB — the backfill
    (a migration over the still-present threads table) is unchanged."""
    from dbops.migration_nodes_parity import backfill_nodes_parity
    db = _fresh(tmp_path)
    pid = db.create_project("Proj", "obj")
    tid = db.create_thread("topic", session_id="s1")  # conversation node only
    with db._connect() as conn:
        # Legacy prod row: a threads row carrying project_id, and a conversation
        # node whose project_id is (still) NULL.
        conn.execute(
            "INSERT INTO threads (id, session_id, topic, status, project_id, "
            "created_at, last_active) VALUES (?, 's1', 'topic', 'active', ?, ?, ?)",
            (tid, pid, _T, _T),
        )
        conn.execute("UPDATE nodes SET project_id=NULL WHERE id=?", (tid,))
        conn.commit()
        backfill_nodes_parity(conn)
        conn.commit()
        row = conn.execute("SELECT project_id FROM nodes WHERE id=?", (tid,)).fetchone()
    assert row[0] == pid


# ── project-keyed conversation reads now resolve from nodes ────────────────────

def test_synth_project_reads_conversation_titles_from_nodes(tmp_path):
    """synth_project reads conversation titles from nodes (kind='conversation'),
    not threads.topic — proven by diverging the node title from the threads row."""
    from juggle_cmd_projects import synth_project
    db = _fresh(tmp_path)
    pid = db.create_project("Dev", "Build")
    tid = db.create_thread("OLD_THREAD_TOPIC", session_id="s1")
    db.update_thread(tid, project_id=pid, assigned_by="human")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET title='NODE_TITLE_X' WHERE id=?", (tid,))
        conn.commit()
    captured = {}

    def fake_llm(prompt, **kw):
        captured["p"] = prompt
        return "Desc.\nKEYWORDS: a\nNOT: b"

    with patch("juggle_cmd_projects.llm_call", side_effect=fake_llm):
        synth_project(db, pid)
    assert "NODE_TITLE_X" in captured["p"]
    assert "OLD_THREAD_TOPIC" not in captured["p"]


def test_check_and_resynth_reads_conversations_from_nodes(tmp_path):
    """check_and_resynth_if_drifted counts a project's conversations from nodes.
    Seeded as node-only rows (no threads) — pre-flip the threads read finds <3 and
    returns early (synth never fires); post-flip it reads the 3 nodes and proceeds."""
    from juggle_cmd_projects import check_and_resynth_if_drifted
    db = _fresh(tmp_path)
    pid = db.create_project("Dev", "Build")
    with db._connect() as conn:
        for i in range(3):
            seed_node(conn, id=f"c{i}", kind="conversation", title=f"node topic {i}",
                      project_id=pid, show_in_list=1, last_active_at=_T)
        conn.commit()
    db.set_match_profile(pid, "Dev. KEYWORDS: x. NOT: y")
    with patch("juggle_cmd_projects.synth_project") as mock_synth, \
         patch("juggle_cmd_projects.drift_score", return_value=0.9):
        check_and_resynth_if_drifted(db, pid, threshold=0.5)
    mock_synth.assert_called_once_with(db, pid)


def test_get_threads_by_project_reads_nodes(tmp_path):
    """get_threads_by_project resolves conversations from nodes (state/title), not
    threads (status/topic) — proven by diverging the node from its threads row."""
    db = _fresh(tmp_path)
    pid = db.create_project("Dev", "Build")
    tid = db.create_thread("OLD_TOPIC", session_id="s1")
    db.update_thread(tid, project_id=pid)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET title='NODE_TITLE_Y', state='running' WHERE id=?", (tid,))
        conn.commit()
    rows = db.get_threads_by_project(pid)
    assert any(r.get("title") == "NODE_TITLE_Y" for r in rows)
    assert any(r.get("state") == "running" for r in rows)


def test_resweep_inbox_resolves_inbox_conversations_from_nodes(tmp_path):
    """resweep_inbox selects INBOX conversations from nodes (project_id='INBOX' via
    the mirror fix). Pre-fix the node's project_id was NULL, so the nodes read found
    nothing and the INBOX thread was never reclassified."""
    from juggle_cmd_projects import resweep_inbox
    db = _fresh(tmp_path)
    pid = db.create_project("Dev", "Build software")
    tid = db.create_thread("software task", session_id="s1")
    with patch("juggle_cmd_projects.infer_project_id", return_value=(pid, 0.85)):
        resweep_inbox(db, limit=10)
    assert db.get_thread(tid)["project_id"] == pid
