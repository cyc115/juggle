"""Tests for `juggle graph learn <node-id> "<text>"` — the node-level learnings
write primitive (spec 2026-07-03-graph-node-learnings §2 + Final revisions 8).

Contract: upsert nodes.learnings; ≤300 chars enforced verbatim (NEVER truncated);
empty text, too-long text, and unknown node all reject fail-loud with a nonzero
exit; text stored exactly as given (verbatim); overwrite-on-rewrite.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common

    monkeypatch.setattr(common, "get_db", lambda: d)
    # Force direct-write path (not the agent-context spool early-return) so the
    # existence check + verbatim write are exercised here.
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return d


def _mk_task(db, task_id="t1", project="INBOX"):
    g.create_task(db, task_id=task_id, project_id=project, title=task_id, prompt="p")


def _learn(db, node_id, text):
    cg.cmd_graph_learn(argparse.Namespace(
        node_id=node_id, text=text, db_path=str(db.db_path)))


def _read_learnings(db, node_id):
    with db._connect() as c:
        row = c.execute("SELECT learnings FROM nodes WHERE id=?", (node_id,)).fetchone()
    return row["learnings"] if row else None


def test_learn_upserts_verbatim(db):
    _mk_task(db)
    _learn(db, "t1", "  keep the leading space; upsert not append  ")
    assert _read_learnings(db, "t1") == "  keep the leading space; upsert not append  "


def test_learn_overwrites_on_rewrite(db):
    _mk_task(db)
    _learn(db, "t1", "first note")
    _learn(db, "t1", "refined note")
    assert _read_learnings(db, "t1") == "refined note"


def test_learn_at_exactly_300_chars_accepted(db):
    _mk_task(db)
    text = "x" * 300
    _learn(db, "t1", text)
    assert _read_learnings(db, "t1") == text


def test_learn_over_300_rejects_failloud_nonzero(db, capsys):
    _mk_task(db)
    text = "y" * 301
    with pytest.raises(SystemExit) as exc:
        _learn(db, "t1", text)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "learnings too long (301/300)" in err
    assert "compress" in err
    # never truncated / never stored
    assert _read_learnings(db, "t1") is None


def test_learn_empty_text_rejects_nonzero(db):
    _mk_task(db)
    with pytest.raises(SystemExit) as exc:
        _learn(db, "t1", "")
    assert exc.value.code != 0
    assert _read_learnings(db, "t1") is None


def test_learn_whitespace_only_text_rejects_nonzero(db):
    _mk_task(db)
    with pytest.raises(SystemExit) as exc:
        _learn(db, "t1", "   \n  ")
    assert exc.value.code != 0
    assert _read_learnings(db, "t1") is None


def test_learn_unknown_node_rejects_nonzero(db):
    with pytest.raises(SystemExit) as exc:
        _learn(db, "nope", "some learning")
    assert exc.value.code != 0


def test_learn_parser_registered():
    """`graph learn <node-id> <text>` must be wired into the graph parser."""
    from juggle_cli_spec import build_parser
    from juggle_cli_commands_misc import MISC_COMMANDS  # noqa: F401
    import argparse as _ap

    parser = _ap.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    from juggle_graph_cli_parsers import register_graph_parsers
    register_graph_parsers(sub)
    args = parser.parse_args(["graph", "learn", "t1", "a decision + why"])
    assert args.node_id == "t1"
    assert args.text == "a decision + why"
    assert args.func is cg.cmd_graph_learn


def test_learn_spools_in_agent_context(tmp_path, monkeypatch):
    """In agent context the write must spool (single-writer broker), then a
    watchdog drain replays it onto the shared DB verbatim."""
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    _mk_task(d)
    # Replayed spool args carry no db_path → cg.get_db(None) resolves the default
    # DB in production; pin it to the test DB here so the drain lands on `d`.
    monkeypatch.setattr("juggle_cmd_graph.get_db", lambda *a, **k: d)

    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr("juggle_spool_paths.spool_dir", lambda: spool)
    monkeypatch.setattr("juggle_spool_apply.spool_dir", lambda: spool)
    # agent context: not orchestrator, flagged agent
    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    cg.cmd_graph_learn(argparse.Namespace(node_id="t1", text="gotcha noted", db_path=str(d.db_path)))
    # nothing written directly yet
    assert _read_learnings(d, "t1") is None

    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")  # drain as watchdog
    from juggle_spool_apply import drain_spool
    stats = drain_spool(d)
    assert stats["applied"] == 1
    assert _read_learnings(d, "t1") == "gotcha noted"
