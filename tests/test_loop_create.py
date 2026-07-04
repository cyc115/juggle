"""Phase-4 pins: transactional single-topic loop create + partition validator
(loop-entity V1, 2026-07-04).

Two critique axes are pinned here:
  * §Axis-5 (atomic create): creating a loop (allocate L-id, kind='loop' project,
    load the single-topic template graph, insert the loop row with next_run) is ONE
    DB transaction — any step failing rolls the WHOLE thing back so NO half-created
    kind='loop' project can claim a P-slot and NO orphan loop/graph row survives.
  * §Axis-4 (deterministic partition): the "same (role, model, delivery) → one
    topic" rule is enforced by CODE at create time, never trusted from the LLM. For
    V1 the validator additionally rejects any multi-topic template.

Plus the §11 bullet-2 guard: a kind='loop' project must NOT leak into the arming /
P-slot feeds before the V2 render band exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import juggle_cmd_loop_create as lc  # noqa: E402
from juggle_cmd_loop_create import create_loop_atomic, compute_next_run  # noqa: E402
from juggle_loop_template_validator import (  # noqa: E402
    LoopTemplateError,
    validate_loop_template,
)


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-create.db"))
    d.init_db()
    return d


def _single_topic_template(delivery="deliver", role="researcher", ntasks=1):
    tasks = [
        {"id": f"t{i}", "title": f"task {i}", "prompt": f"do {i}",
         "role": role, "model": "sonnet", "verify_cmd": None, "deps": []}
        for i in range(ntasks)
    ]
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": delivery, "tasks": tasks,
    }]}


# ── Deterministic partition validator (§Axis-4) ─────────────────────────────────
def test_validator_rejects_multi_topic_in_v1():
    """V1 single-topic scope guard: a 2-topic template is rejected deterministically."""
    tmpl = {"topics": [
        {"id": "a", "title": "A", "delivery": "merge",
         "tasks": [{"id": "x", "title": "x", "prompt": "p", "role": "coder"}]},
        {"id": "b", "title": "B", "delivery": "merge",
         "tasks": [{"id": "y", "title": "y", "prompt": "p", "role": "coder"}]},
    ]}
    with pytest.raises(LoopTemplateError, match="single-topic"):
        validate_loop_template(tmpl)


def test_validator_rejects_mixed_role_topic():
    """§Axis-4: a topic mixing researcher+coder member tasks is rejected in code —
    the partition rule is NOT trusted from raw LLM output."""
    tmpl = {"topics": [{
        "id": "a", "title": "A", "delivery": "merge",
        "tasks": [
            {"id": "x", "title": "x", "prompt": "p", "role": "researcher"},
            {"id": "y", "title": "y", "prompt": "p", "role": "coder"},
        ],
    }]}
    with pytest.raises(LoopTemplateError, match="mixes"):
        validate_loop_template(tmpl)


def test_validator_normalizes_single_topic():
    """A valid single-topic template lifts the shared role/delivery onto the topic
    and fills each task's role/delivery/deps."""
    norm = validate_loop_template(_single_topic_template(delivery="deliver",
                                                         role="researcher", ntasks=2))
    topic = norm["topics"][0]
    assert topic["role"] == "researcher"
    assert topic["delivery"] == "deliver"
    assert all(tk["role"] == "researcher" and tk["delivery"] == "deliver"
               for tk in topic["tasks"])


def test_validator_rejects_dangling_and_self_deps():
    """Dep closure (code-review #4): a dep to a non-member id or to itself is
    rejected — a dangling dep edge would silently wedge the task."""
    tmpl = _single_topic_template(ntasks=2)
    tmpl["topics"][0]["tasks"][1]["deps"] = ["nonexistent"]
    with pytest.raises(LoopTemplateError, match="not a member task"):
        validate_loop_template(tmpl)
    tmpl2 = _single_topic_template(ntasks=1)
    tmpl2["topics"][0]["tasks"][0]["deps"] = [tmpl2["topics"][0]["tasks"][0]["id"]]
    with pytest.raises(LoopTemplateError, match="depends on itself"):
        validate_loop_template(tmpl2)


def test_compute_next_run_from_cadence():
    """next_run is derived from the cadence string; an unparseable cadence AND an
    out-of-range daily time are both fail-loud LoopTemplateError (a loop with no
    valid timer would silently never fire)."""
    from datetime import datetime
    now = datetime(2026, 7, 4, 8, 0, 0)
    assert compute_next_run("every 15m", now).startswith("2026-07-04T08:15")
    assert compute_next_run("daily at 09:30", now).startswith("2026-07-04T09:30")
    with pytest.raises(LoopTemplateError):
        compute_next_run("whenever", now)
    with pytest.raises(LoopTemplateError, match="invalid time"):
        compute_next_run("daily at 30:70", now)  # code-review #3: range guard


# ── Atomic create (§Axis-5) ─────────────────────────────────────────────────────
def test_loop_project_has_kind_loop(db):
    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")
    proj = db.get_project(r["project_id"])
    assert proj["kind"] == "loop"


def test_loop_create_sets_next_run(db):
    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 30m")
    loop = db.get_loop(r["loop_id"])
    assert loop["next_run"] == r["next_run"]
    assert loop["next_run"]  # populated, not NULL


def test_run_seq_namespaced_node_ids(db):
    """Created node ids carry the <L#>-r0- prefix (collision guard vs the
    guarded-upsert refusal on re-fire)."""
    r = create_loop_atomic(db, template=_single_topic_template(ntasks=2),
                           cadence="every 1h")
    prefix = f"{r['loop_id']}-r0-"
    assert r["topic_id"].startswith(prefix)
    assert all(nid.startswith(prefix) for nid in r["node_ids"])


def test_loop_create_atomic_rollback_on_graph_failure(db, monkeypatch):
    """§Axis-5 LOAD-BEARING: a failure mid-create (after the project + topic are
    written) rolls the WHOLE transaction back — NO projects row, NO loops row, NO
    orphan nodes. A half-created kind='loop' project must never claim a P-slot."""
    def _boom(*a, **k):
        raise RuntimeError("injected mid-create failure")
    monkeypatch.setattr(lc.db_graph, "create_task", _boom)

    with pytest.raises(RuntimeError, match="injected"):
        create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")

    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM projects WHERE kind='loop'").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM loops").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE id LIKE '%-r0-%'").fetchone()[0] == 0


def test_loop_project_excluded_from_arming_feeds(db):
    """§11 bullet-2 guard: a kind='loop' project must NOT surface in the arming /
    P-slot feeds (list_projects default + get_active_projects), while a normal
    project still does. Closes the leak before the V2 render band lands."""
    normal = db.create_project(name="normal", objective="o")
    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")

    active_ids = {p["id"] for p in db.get_active_projects()}
    listed_ids = {p["id"] for p in db.list_projects()}
    assert r["project_id"] not in active_ids
    assert r["project_id"] not in listed_ids
    assert normal in active_ids and normal in listed_ids  # normal project unaffected
    # include_archived='give me everything' still sees the loop (doctor migrate path)
    assert r["project_id"] in {p["id"] for p in db.list_projects(include_archived=True)}
    # match-profile synth feed excludes loops too (code-review #5)
    db.mark_project_dirty(r["project_id"])
    assert r["project_id"] not in {p["id"] for p in db.get_dirty_projects()}


def test_loop_project_excluded_from_cockpit_render(db):
    """code-review #2: the cockpit project-slot / DAG feeds must NOT render a
    kind='loop' project (the plan's Phase-4 P-slot pin). A loop with a topic graph
    produces no graph-DAG card."""
    from juggle_cockpit_graph_dag import load_graph_dags

    normal = db.create_project(name="normal", objective="o")
    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")
    with db._connect() as conn:
        dag_pids = {d.project_id for d in load_graph_dags(conn)}
    assert r["project_id"] not in dag_pids
    _ = normal  # a normal project with tasks WOULD render; loop must not
