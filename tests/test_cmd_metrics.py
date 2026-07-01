"""juggle metrics CLI (2026-06-30 orchestration-metrics Task 9)."""
import json
from argparse import Namespace

import juggle_cmd_metrics as cm


def _seed(juggle_db, **kw):
    tid = juggle_db.create_thread(topic="t", session_id="s")
    base = dict(thread_id=tid, input_prompt="p", agent_id=None, role="coder",
                model="m", harness="claude", project_id="INBOX", topic_id=None, task_id="t1")
    base.update(kw)
    return juggle_db.insert_agent_run(**base)


def test_metrics_json_smoke(juggle_db, capsys):
    """2026-06-30 orchestration-metrics: `juggle metrics --json` emits slice structure."""
    _seed(juggle_db)
    cm.cmd_metrics(Namespace(since=None, by=None, json_out=True,
                             db_path=str(juggle_db.db_path)))
    data = json.loads(capsys.readouterr().out)
    assert "slices" in data and "all" in data["slices"]
    assert set(data["slices"]["all"]) == {"cost", "performance", "quality"}


def test_metrics_by_role(juggle_db, capsys):
    _seed(juggle_db, role="planner")
    cm.cmd_metrics(Namespace(since=None, by="role", json_out=True,
                             db_path=str(juggle_db.db_path)))
    data = json.loads(capsys.readouterr().out)
    assert "planner" in data["slices"]


def test_metrics_table_smoke(juggle_db, capsys):
    _seed(juggle_db)
    cm.cmd_metrics(Namespace(since=None, by=None, json_out=False,
                             db_path=str(juggle_db.db_path)))
    out = capsys.readouterr().out
    assert "token_coverage" in out or "coverage" in out.lower()
