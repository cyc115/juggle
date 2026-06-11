"""Tests for reconcile_topic_state / reconcile_project_topics (B1 + B3).

Regression pin (2026-06-11 bug J): topic tier drifts from node tier — graph
mark-task advances nodes but topics stay phantom 'running'. reconcile repairs
the drift; B1 wires it into mark-task write path; B3 exposes it as a CLI
subcommand and doctor hook.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    monkeypatch.setattr(common, "get_db", lambda *a, **kw: d)
    return d


def _mk_project(db, pid="P1"):
    db.create_project(pid, pid, "test project")
    return pid


def _mk_topic(db, topic_id, project_id, state="pending"):
    t.create_topic(db, topic_id=topic_id, project_id=project_id, title=topic_id)
    if state != "pending":
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state=? WHERE id=?", (state, topic_id)
            )
            conn.commit()


def _mk_node(db, node_id, project_id, topic_id, state="pending"):
    g.create_node(db, node_id=node_id, project_id=project_id, title=node_id,
                  prompt=f"do {node_id}")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET topic_id=? WHERE id=?", (topic_id, node_id)
        )
        conn.commit()
    if state != "pending":
        # Walk the node to the desired state via mark_completion
        if state == "verified":
            g.mark_completion(db, node_id, integrate_ok=True, verify_ok=True)
        elif state == "failed-verify":
            g.mark_completion(db, node_id, integrate_ok=True, verify_ok=False)
        elif state == "failed-exec":
            g.mark_exec_failed(db, node_id)
        elif state in ("running", "dispatching", "integrating"):
            # manually set for simplicity
            with db._connect() as conn:
                conn.execute(
                    "UPDATE graph_nodes SET state=? WHERE id=?", (state, node_id)
                )
                conn.commit()


# ── reconcile_topic_state unit tests ──────────────────────────────────────────


def test_reconcile_sets_topic_verified_when_all_nodes_verified(db):
    """All member nodes verified → topic becomes verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_node(db, "n2", pid, "T1", state="verified")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "verified"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "verified"
    assert topic["verified_at"] is not None


def test_reconcile_idempotent_on_already_verified_topic(db):
    """Re-running reconcile on an already-verified topic leaves it unchanged.

    Regression pin (2026-06-11 bug J): idempotency required so repeated reconcile
    calls don't corrupt terminal topics.
    """
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="verified")
    _mk_node(db, "n1", pid, "T1", state="verified")

    result1 = t.reconcile_topic_state(db, "T1")
    result2 = t.reconcile_topic_state(db, "T1")

    assert result1 == "verified"
    assert result2 == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_clears_phantom_running_to_verified(db):
    """Exact prod bug (2026-06-11 bug J): topic stored 'running' but all member
    nodes verified → reconcile sets topic to verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom — agent died
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_node(db, "n2", pid, "T1", state="verified")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_failed_member_sets_topic_failed_verify(db):
    """Any member node in a failed state → topic becomes failed-verify."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_node(db, "n2", pid, "T1", state="failed-verify")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "failed-verify"
    assert t.get_topic(db, "T1")["state"] == "failed-verify"


def test_reconcile_running_member_sets_topic_running(db):
    """Any member node running/dispatching/integrating → topic becomes running."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_node(db, "n2", pid, "T1", state="running")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "running"
    assert t.get_topic(db, "T1")["state"] == "running"


# ── B1: write-path sync via cmd_graph_mark_task ───────────────────────────────


def test_mark_task_last_node_flips_topic_verified(db):
    """B1 regression pin (2026-06-11 bug J): marking the last unverified node
    of a topic via 'graph mark-task' must atomically flip the owning topic to
    verified — node tier and topic tier must never drift after mark-task."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_node(db, "n2", pid, "T1", state="pending")  # last unverified

    args = SimpleNamespace(
        task_id="n2", fail=False, handoff=None,
        db_path=str(db.db_path),
    )
    cg.cmd_graph_mark_task(args)

    node = g.get_node(db, "n2")
    assert node["state"] == "verified"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "verified", (
        f"topic tier drifted: expected 'verified', got {topic['state']!r}"
    )


# ── B3: reconcile CLI subcommand ──────────────────────────────────────────────


def test_graph_reconcile_cli_corrects_drifted_topic(db, capsys):
    """'juggle graph reconcile <project>' fixes drifted topic states."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom running
    _mk_node(db, "n1", pid, "T1", state="verified")
    _mk_topic(db, "T2", pid, state="pending")
    _mk_node(db, "n2", pid, "T2", state="pending")

    args = SimpleNamespace(project=pid, json_out=False, db_path=str(db.db_path))
    cg.cmd_graph_reconcile(args)

    out = capsys.readouterr().out
    assert "T1" in out
    assert "running" in out
    assert "verified" in out
    assert t.get_topic(db, "T1")["state"] == "verified"
    assert t.get_topic(db, "T2")["state"] == "pending"  # unchanged


def test_graph_reconcile_cli_json_output(db, capsys):
    """'juggle graph reconcile --json' emits valid JSON with before/after."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")
    _mk_node(db, "n1", pid, "T1", state="verified")

    args = SimpleNamespace(project=pid, json_out=True, db_path=str(db.db_path))
    cg.cmd_graph_reconcile(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "T1" in data
    assert data["T1"]["before"] == "running"
    assert data["T1"]["after"] == "verified"


# ── B3: doctor runs reconcile ─────────────────────────────────────────────────


def test_doctor_reconciles_drifted_topic(db, tmp_path, monkeypatch):
    """'juggle doctor' repairs drifted topic states (B3 repair valve).

    Regression pin (2026-06-11 bug J): after doctor runs, no topic should have
    a state that contradicts its member nodes.
    """
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom
    _mk_node(db, "n1", pid, "T1", state="verified")

    import juggle_db
    import juggle_cmd_doctor as doc
    monkeypatch.setattr(juggle_db, "DB_PATH", str(db.db_path))
    # Suppress config migration noise by making CONFIG_PATH point to non-existent file
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "no-config.json")

    args = SimpleNamespace(dry_run=False)
    doc.cmd_doctor(args)

    assert t.get_topic(db, "T1")["state"] == "verified"
