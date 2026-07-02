"""Headless verification for cockpit-gitlog-view: `cockpit --out --graph-full`
and `--screenshot ... --graph-full` (T-cockpit-gitlog-view acceptance)."""
from datetime import datetime, timezone


def _seed(db_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Proj", "active", now, now),
        )
        conn.execute(
            "INSERT INTO nodes(id,kind,title,objective,state,project_id,merged_sha,"
            "verified_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("a", "topic", "A", "do a", "verified", "P", "6db0e75abc",
             "2026-07-02T09:12:00+00:00", now, now),
        )
        conn.commit()
    return db


def test_render_gitlog_static_shows_row_and_legend(tmp_path):
    from juggle_cockpit_static import render_gitlog_static

    db_path = str(tmp_path / "juggle.db")
    _seed(db_path)
    text = render_gitlog_static(db_path=db_path)
    assert "a" in text
    assert "6db0e75" in text
    assert "waits on" in text  # graph_inline_legend footer


def test_render_gitlog_static_empty_dag_says_no_tasks(tmp_path):
    from juggle_cockpit_static import render_gitlog_static
    from juggle_db import JuggleDB

    db_path = str(tmp_path / "juggle.db")
    JuggleDB(db_path=db_path).init_db()
    text = render_gitlog_static(db_path=db_path)
    assert "no tasks" in text


def test_cockpit_out_graph_full_cli(tmp_path):
    """cockpit --out --graph-full renders the full-screen view via subprocess."""
    import subprocess
    import sys
    from pathlib import Path

    db_path = str(tmp_path / "juggle.db")
    _seed(db_path)
    script = Path(__file__).resolve().parent.parent / "src" / "juggle_cockpit.py"
    r = subprocess.run(
        [sys.executable, str(script), "--db", db_path, "--out", "--graph-full"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "a" in r.stdout
    assert "6db0e75" in r.stdout


def test_screenshot_graph_full_writes_svg(tmp_path):
    from pathlib import Path
    from juggle_cockpit_screenshot import save_screenshot

    db_path = str(tmp_path / "juggle.db")
    _seed(db_path)
    out = save_screenshot(str(tmp_path / "out.svg"), db_path, graph_full=True)
    assert Path(out).exists()
