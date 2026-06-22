"""P8 readiness gate pins (incident 2026-06-22): `juggle doctor` must refuse to
drop the legacy tables (threads/graph_*) until `nodes` fully mirrors them
(id-anchored anti-join == 0) AND integrity holds, and the static scanner must
report every live source line still targeting a legacy table."""
import argparse
import json
import sqlite3  # noqa: F401 — kept for parity with predicate's sqlite usage

import pytest  # noqa: F401 — imported for future pin parametrization

from juggle_db import JuggleDB
from helpers.node_seed import seed_thread, seed_node


def _fresh(tmp_path):
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


# ── Gate B: p8_drop_ready runtime data-readiness predicate ────────────────────

def test_drop_ready_false_when_thread_unmirrored(tmp_path):
    from dbops.p8_readiness import p8_drop_ready
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_thread(conn, id="t1")
        conn.commit()
        ready, reasons = p8_drop_ready(conn)
    assert ready is False
    assert any("threads" in r for r in reasons)


def test_drop_ready_true_when_fully_mirrored(tmp_path):
    from dbops.p8_readiness import p8_drop_ready
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_thread(conn, id="t1")
        seed_node(conn, id="t1", kind="conversation")
        conn.commit()
        ready, reasons = p8_drop_ready(conn)
    assert ready is True, reasons


def test_drop_ready_already_dropped(tmp_path):
    db = _fresh(tmp_path)
    from dbops.p8_readiness import p8_drop_ready
    with db._connect() as conn:
        for t in ("graph_edges", "graph_tasks", "graph_topics", "threads"):
            conn.execute(f"DROP TABLE {t}")
        conn.commit()
        ready, reasons = p8_drop_ready(conn)
    assert ready is False and reasons == ["already-dropped"]


def test_drop_ready_false_on_null_title(tmp_path):
    """A NULL `nodes.title` must block the drop. The live schema enforces
    NOT NULL, so rebuild `nodes` without that guard to inject the row the
    predicate must reject (UPDATE-to-NULL is itself blocked by SQLite)."""
    db = _fresh(tmp_path)
    from dbops.p8_readiness import p8_drop_ready
    with db._connect() as conn:
        conn.execute("ALTER TABLE nodes RENAME TO _nodes_old")
        conn.execute(
            "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
            "state TEXT, parent_id TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO nodes (id,kind,title,state,created_at,updated_at) "
                     "VALUES ('n1','task',NULL,'open','x','x')")
        conn.commit()
        ready, reasons = p8_drop_ready(conn)
    assert ready is False and any("NULL title" in r for r in reasons)


# ── Gate A: scan_legacy_refs static source-ref scanner ────────────────────────

def test_scan_legacy_refs_live_only(tmp_path):
    from dbops.p8_readiness import scan_legacy_refs
    root = tmp_path / "src"
    (root / "dbops").mkdir(parents=True)
    (root / "live.py").write_text("x = conn.execute('SELECT id FROM threads WHERE id=?')\n")
    (root / "dbops" / "schema.py").write_text("CREATE = 'CREATE TABLE threads (id TEXT)'\n")
    (root / "dbops" / "migration_x.py").write_text("conn.execute('DROP TABLE graph_tasks')\n")
    (root / "dbops" / "p8_readiness.py").write_text("LEFT JOIN graph_topics g ON g.id=n.id\n")
    (root / "commented.py").write_text("# legacy: SELECT * FROM graph_topics note\n")
    refs = scan_legacy_refs(root)
    files = {r.file.name for r in refs}
    assert files == {"live.py"}      # all excluded except the one live ref
    assert len(refs) == 1


def test_scan_legacy_refs_matches_three_tables(tmp_path):
    from dbops.p8_readiness import scan_legacy_refs
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text(
        "a='UPDATE threads SET x=1'\nb='FROM graph_topics'\nc='JOIN graph_tasks t'\n")
    assert len(scan_legacy_refs(root)) == 3


# ── Gate A+B: combined report + doctor --pre-p8-check wiring ───────────────────

def test_pre_p8_report_shape(tmp_path):
    from dbops.p8_readiness import pre_p8_report
    db = _fresh(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "live.py").write_text("x='FROM threads'\n")
    with db._connect() as conn:
        seed_thread(conn, id="t1")       # unmirrored -> runtime blocked
        conn.commit()
        rep = pre_p8_report(conn, src)
    assert rep["static"]["fail"] == 1
    assert rep["runtime"]["ready"] is False
    assert rep["pass"] is False


def test_doctor_pre_p8_check_exit_nonzero(tmp_path, monkeypatch, capsys):
    import juggle_db
    import juggle_cmd_doctor
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_thread(conn, id="t1")       # unmirrored
        conn.commit()
    monkeypatch.setattr(juggle_db, "DB_PATH", str(tmp_path / "j.db"))
    rc = juggle_cmd_doctor.cmd_doctor(
        argparse.Namespace(pre_p8_check=True, json_out=True))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["pass"] is False
