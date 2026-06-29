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


def test_scan_excludes_reverse_backfill_inverse(tmp_path):
    """2026-06-29 P8 c4-write-cut: p8_reverse_backfill.py is the sanctioned Step-4
    rollback inverse (writes graph_tasks/graph_edges by design, dead in the forward
    path). The static gate must NOT count it as a steady-state legacy violation —
    same exclusion class as p8_readiness.py."""
    from dbops.p8_readiness import scan_legacy_refs
    root = tmp_path / "src" / "dbops"
    root.mkdir(parents=True)
    (root / "p8_reverse_backfill.py").write_text(
        "x = conn.execute('INSERT OR IGNORE INTO graph_tasks (id) VALUES (1)')\n")
    assert scan_legacy_refs(tmp_path / "src") == []


def test_scan_legacy_refs_matches_four_tables(tmp_path):
    """graph_edges is a legacy table too — the scanner must catch a live writer of
    it (its omission hid the db_graph_edges.replace_edges dual-write, P8 terminal)."""
    from dbops.p8_readiness import scan_legacy_refs
    root = tmp_path / "src"
    root.mkdir()
    (root / "m.py").write_text(
        "a='UPDATE threads SET x=1'\nb='FROM graph_topics'\n"
        "c='JOIN graph_tasks t'\nd='INSERT INTO graph_edges (task_id) VALUES (1)'\n")
    assert len(scan_legacy_refs(root)) == 4


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


def test_gate_a_reports_exclusions_and_imports():
    """2026-06-29 P8 M4: the gate must LOG which files it skipped (excluded_files)
    and assert the retired legacy engines are unreachable (import_refs==0) — no
    PASS:0 while db_mirror still imports. Scans the shipped src/ so the report
    reflects the running binary, like doctor --pre-p8-check."""
    from pathlib import Path
    from dbops.p8_readiness import pre_p8_report
    src_root = Path(__file__).resolve().parent.parent / "src"
    rep = pre_p8_report(sqlite3.connect(":memory:"), src_root)
    assert isinstance(rep["static"]["excluded_files"], list)
    # the scan must actually skip the schema/migration + sanctioned-inverse modules:
    assert rep["static"]["excluded_files"], "excluded_files must not be empty"
    assert rep["static"]["import_refs"] == 0   # db_mirror deleted; no legacy-engine imports


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
