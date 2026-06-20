"""Topic-tier graph spec parsing/loading (R9). Legacy flat specs must load
unchanged as synthetic single-task topics (R6)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_upsert import parse_topics_spec, validate_topics  # noqa: E402

TOPIC_SPEC = """\
## topic auth: Authentication
Build login end-to-end.

### t1: DB schema
verify_cmd: pytest tests -q
Create users table.

### t2: Login endpoint
deps: t1
Implement /login.

## topic ui: Frontend
### u1: Login form
deps: t2
Render the form.
"""

LEGACY_SPEC = """\
## n1: First
Do the first thing.

## n2: Second
deps: n1
Do the second thing.
"""


def test_parse_topics_spec_two_tiers():
    topics = parse_topics_spec(TOPIC_SPEC)
    assert [t["id"] for t in topics] == ["auth", "ui"]
    assert topics[0]["objective"].startswith("Build login")
    assert [n["id"] for n in topics[0]["tasks"]] == ["t1", "t2"]
    assert topics[0]["tasks"][1]["deps"] == ["t1"]
    assert topics[1]["tasks"][0]["deps"] == ["t2"]  # cross-topic task dep


def test_legacy_flat_spec_wraps_each_task_in_synthetic_topic():
    """REGRESSION PIN (2026-06-11 R6): existing flat spec files must keep
    loading — each old `## task` becomes a 1-task topic (task ≡ topic)."""
    topics = parse_topics_spec(LEGACY_SPEC)
    assert [t["id"] for t in topics] == ["T-n1", "T-n2"]
    assert all(len(t["tasks"]) == 1 for t in topics)
    assert topics[1]["tasks"][0]["deps"] == ["n1"]


def test_mixed_spec_rejected():
    mixed = TOPIC_SPEC + "\n## stray: Flat task\nprompt\n"
    errors = validate_topics(parse_topics_spec(mixed))
    assert any("mix" in e.lower() for e in errors)


def test_empty_topic_rejected():
    errors = validate_topics(parse_topics_spec("## topic empty: Nothing\nobjective only\n"))
    assert any("no tasks" in e.lower() for e in errors)


def test_cross_topic_cycle_rejected():
    spec = """\
## topic A: a
### a1: x
deps: b1
p
## topic B: b
### b1: y
deps: a1
p
"""
    errors = validate_topics(parse_topics_spec(spec))
    assert any("cycle" in e.lower() for e in errors)


# ── Step 4 wiring: load creates topics + sets topic_id; add-task --topic ────────

import pytest  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g, db_topics  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _load_args(tmp_path, db, spec, project="INBOX"):
    f = tmp_path / "spec.md"
    f.write_text(spec)
    return SimpleNamespace(file=str(f), project=project, db_path=str(db.db_path))


def _add_args(db, **kw):
    base = dict(
        project="INBOX", id="x", title="X", prompt="do x", topic=None,
        deps=None, required_by=None, verify_cmd=None, json_out=False,
        db_path=str(db.db_path),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_load_topic_spec_creates_topics_and_sets_topic_id(db, tmp_path):
    cg.cmd_project_graph_load(_load_args(tmp_path, db, TOPIC_SPEC))
    assert [t["id"] for t in db_topics.list_topics(db, "INBOX")] == ["auth", "ui"]
    assert g.get_task(db, "t1")["topic_id"] == "auth"
    assert g.get_task(db, "u1")["topic_id"] == "ui"


def test_load_legacy_spec_creates_synthetic_topics(db, tmp_path):
    cg.cmd_project_graph_load(_load_args(tmp_path, db, LEGACY_SPEC))
    assert [t["id"] for t in db_topics.list_topics(db, "INBOX")] == ["T-n1", "T-n2"]
    assert g.get_task(db, "n1")["topic_id"] == "T-n1"


def test_add_task_no_topic_on_topic_project_succeeds(db, tmp_path):
    """P6: missing --topic on a real-topic project no longer refuses — routes add_node."""
    cg.cmd_project_graph_load(_load_args(tmp_path, db, TOPIC_SPEC))
    # Should NOT raise (pre-P6 this raised SystemExit with "topic required")
    cg.cmd_graph_add_task(_add_args(db, id="t3", deps="t1", topic=None))
    assert g.get_task(db, "t3") is not None


def test_add_task_with_topic_assigns_it(db, tmp_path):
    cg.cmd_project_graph_load(_load_args(tmp_path, db, TOPIC_SPEC))
    cg.cmd_graph_add_task(_add_args(db, id="t3", deps="t1", topic="auth"))
    assert g.get_task(db, "t3")["topic_id"] == "auth"


def test_add_task_no_topic_on_flat_project_succeeds(db, tmp_path):
    """P6: missing --topic on a flat project routes through add_node (no synthetic topic)."""
    cg.cmd_project_graph_load(_load_args(tmp_path, db, LEGACY_SPEC))
    cg.cmd_graph_add_task(_add_args(db, id="n3", deps="n1", topic=None))
    assert g.get_task(db, "n3") is not None
