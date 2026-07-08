"""juggle brief command — --json shape contracts (2026-07-07 juggle-brief-command).

Drives cmd_brief against the isolated tmp-DB fixture (no live tmux/git needed —
reconciliation and git health degrade to best-effort defaults). Asserts the
deterministic --json shape for session / topic / project modes.
"""
import json
from argparse import Namespace

import juggle_cmd_brief as cb


def _args(db, ident=None, json_out=True):
    return Namespace(id=ident, json_out=json_out, db_path=str(db.db_path))


def _seed_topic(db, title="Refactor scan"):
    tid = db.create_thread(topic=title, session_id="s")
    return tid


def test_session_json_shape(juggle_db, capsys):
    _seed_topic(juggle_db)
    cb.cmd_brief(_args(juggle_db))
    data = json.loads(capsys.readouterr().out)
    assert set(data) >= {
        "version", "watchdog", "autopilot", "unconsumed_notifs",
        "topics", "agents", "actions", "health",
    }
    assert isinstance(data["topics"], list) and data["topics"]
    assert set(data["agents"]) == {"busy", "idle", "busy_list"}
    assert set(data["health"]) >= {"git_branch", "dirty", "worktrees", "selfheal"}


def test_session_json_excludes_archived(juggle_db, capsys):
    tid = _seed_topic(juggle_db, title="Ancient")
    juggle_db.archive_thread(tid)
    _seed_topic(juggle_db, title="Live one")
    cb.cmd_brief(_args(juggle_db))
    data = json.loads(capsys.readouterr().out)
    titles = [t["title"] for t in data["topics"]]
    assert "Ancient" not in titles
    assert "Live one" in titles


def test_topic_json_shape_and_verdict(juggle_db, capsys):
    tid = _seed_topic(juggle_db, title="Topic probe")
    label = juggle_db.get_thread(tid)["user_label"]
    cb.cmd_brief(_args(juggle_db, ident=label))
    data = json.loads(capsys.readouterr().out)
    assert data["thread_id"] == tid
    assert data["verdict"] in {
        "idle-done-unreleased", "dep-wait", "failed",
        "unmerged-work", "no-agent", "healthy-running",
    }
    assert set(data) >= {"id", "state", "agent", "actions", "notifications",
                         "worktree", "verdict"}


def test_topic_unknown_id_exits(juggle_db, capsys):
    import pytest

    with pytest.raises(SystemExit):
        cb.cmd_brief(_args(juggle_db, ident="NOPE"))


def test_project_json_shape(juggle_db, capsys):
    pid = juggle_db.create_project(name="Cockpit", objective="do it")
    cb.cmd_brief(_args(juggle_db, ident=pid))
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == pid
    assert set(data) >= {"id", "name", "armed", "counts", "next_node", "failing_nodes"}
    assert isinstance(data["failing_nodes"], list)


def test_human_session_under_budget(juggle_db, capsys):
    """The no-arg human snapshot stays within the ~1.5k char budget on real
    (tmp-DB) collected state, not just synthetic render input."""
    for i in range(3):
        _seed_topic(juggle_db, title=f"Topic {i}")
    cb.cmd_brief(_args(juggle_db, json_out=False))
    out = capsys.readouterr().out
    assert len(out) <= 1600, f"human snapshot {len(out)} chars"
