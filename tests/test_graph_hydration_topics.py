"""Topic hydration (R9): objective + dep-TOPIC handoffs + SEQUENTIAL task list
+ the per-task mark-task contract. Never thread.summary (DA M4)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_hydration import (  # noqa: E402
    build_source_of_truth_section,
    build_topic_hydration,
)


def _topic():
    return {"id": "auth", "title": "Authentication", "objective": "Login e2e."}


def _tasks():
    return [
        {"id": "t1", "title": "Schema", "prompt": "users table",
         "verify_cmd": "pytest tests -q", "state": "pending"},
        {"id": "t2", "title": "Endpoint", "prompt": "/login",
         "verify_cmd": None, "state": "verified"},
    ]


def test_topic_hydration_contains_contract_and_order():
    payload = build_topic_hydration(
        "Proj objective", _topic(),
        deps=[{"id": "db", "title": "DB", "handoff": "schema v1", "diffstat": None}],
        tasks=_tasks(),
    )
    assert "Proj objective" in payload.context and "Login e2e." in payload.context
    assert "schema v1" in payload.context             # dep TOPIC handoff
    assert [t.id for t in payload.tasks] == ["t1", "t2"]  # sequential order preserved
    assert payload.tasks[0].verify_cmd == "pytest tests -q"
    # Lifecycle/finalize (mark-task, agent complete) is rendered centrally by
    # juggle_dispatch_core via render_agent_prompt — not this pure builder
    # (PC2, Agent Prompt Contract v2).


def test_verified_task_flagged_for_skip():
    payload = build_topic_hydration("", _topic(), deps=[], tasks=_tasks())
    t2 = next(t for t in payload.tasks if t.id == "t2")
    assert t2.verified is True


# ── gl-hydrate: topic-level learnings injection ────────────────────────────────


def _learnings(*triples):
    """triples of (node_id, text, completed_at) → read_learnings-shaped items."""
    return [{"node_id": n, "text": t, "completed_at": c} for n, t, c in triples]


def test_dep_learnings_injected_capped_with_breadcrumb():
    # 12 dep-topic learnings, caller-ordered most-recent-first; cap 10 + breadcrumb.
    dep_learnings = _learnings(
        *[(f"n{i}", f"gotcha {i}", f"2026-07-03T{i:02d}:00:00") for i in range(12)]
    )
    payload = build_topic_hydration(
        "Obj", _topic(),
        deps=[{"id": "db", "title": "DB", "handoff": "schema v1", "diffstat": None}],
        tasks=_tasks(), dep_learnings=dep_learnings,
    )
    ctx = payload.context
    assert "n0: gotcha 0" in ctx and "n9: gotcha 9" in ctx   # first 10 shown
    assert "n10: gotcha 10" not in ctx and "n11: gotcha 11" not in ctx
    assert "(+2 more via 'graph learnings --topic')" in ctx  # nothing silently dropped


def test_dep_learnings_delineated_from_handoff():
    payload = build_topic_hydration(
        "Obj", _topic(),
        deps=[{"id": "db", "title": "DB", "handoff": "schema v1", "diffstat": None}],
        tasks=_tasks(),
        dep_learnings=_learnings(("n1", "watch the FK order", "2026-07-03T05:00:00")),
    )
    ctx = payload.context
    assert "schema v1" in ctx                       # handoff channel intact
    assert "watch the FK order" in ctx              # learnings channel
    assert "learnings" in ctx.lower()               # labelled section
    assert ctx.index("schema v1") < ctx.index("watch the FK order")  # handoff above learnings


def test_own_learnings_injected_on_redispatch():
    payload = build_topic_hydration(
        "Obj", _topic(), deps=[], tasks=_tasks(),
        own_learnings=_learnings(("auth-k0", "prior run: mock the clock", "2026-07-03T06:00:00")),
    )
    ctx = payload.context
    assert "prior run: mock the clock" in ctx and "auth-k0" in ctx


def test_no_dep_learnings_scaffolding_when_absent():
    ctx = build_topic_hydration("Obj", _topic(), deps=[], tasks=_tasks()).context
    assert "Upstream learnings" not in ctx           # no empty dep block
    assert "own prior learnings" not in ctx          # no empty own block


def test_learnings_instruction_line_always_present():
    ctx = build_topic_hydration("Obj", _topic(), deps=[], tasks=_tasks()).context
    assert "graph learnings --topic" in ctx          # pull broader context
    assert "graph learn " in ctx                     # record own before completing


def test_repair_prompt_injects_own_learnings():
    from juggle_graph_hydration import build_repair_prompt

    p = build_repair_prompt(
        {"class": "red-suite"},
        own_learnings=_learnings(("x-k0", "the clock must be mocked", "2026-07-03T08:00:00")),
    )
    assert "the clock must be mocked" in p and "own prior learnings" in p.lower()
    # absent → no dangling section
    assert "own prior learnings" not in build_repair_prompt({"class": "red-suite"}).lower()


def test_hydrate_for_topic_aggregates_dep_and_own_learnings(tmp_path):
    from juggle_db import JuggleDB
    from dbops import db_graph as g, db_topics as t
    from juggle_graph_hydration import hydrate_for_topic

    db = JuggleDB(db_path=str(tmp_path / "g.db"))
    db.init_db()

    # Dep topic D: two verified tasks with learnings (D-k1 completed later → newer).
    t.create_topic(db, topic_id="D", project_id="P", title="Dep")
    for nid, txt, va in [("D-k0", "dep gotcha old", "2026-07-03T01:00:00"),
                         ("D-k1", "dep gotcha new", "2026-07-03T09:00:00")]:
        g.create_task(db, task_id=nid, project_id="P", title=nid, prompt="p")
        g.set_task_topic(db, nid, "D")
        g.set_learnings(db, nid, txt)
        with db._connect() as c:
            c.execute("UPDATE nodes SET verified_at=? WHERE id=?", (va, nid))
            c.commit()

    # Main topic M depends on D (cross-topic edge) and carries its OWN learning.
    t.create_topic(db, topic_id="M", project_id="P", title="Main")
    g.create_task(db, task_id="M-k0", project_id="P", title="M-k0", prompt="p")
    g.set_task_topic(db, "M-k0", "M")
    g.replace_edges(db, "M-k0", ["D-k1"])       # cross-topic dep M → D
    g.set_learnings(db, "M-k0", "own retry note")

    ctx = hydrate_for_topic(db, "P", t.get_topic(db, "M")).context
    assert "dep gotcha new" in ctx and "dep gotcha old" in ctx      # aggregated from dep topic
    assert ctx.index("dep gotcha new") < ctx.index("dep gotcha old")  # most-recent-first
    assert "own retry note" in ctx                                  # topic's OWN learning
    assert "graph learnings --topic" in ctx                         # instruction line


# ── T-fix-dispatch-plan-spec-provision: Source of truth (READ FIRST) section ───


def test_source_of_truth_section_with_both_paths():
    section = build_source_of_truth_section(
        {"plan_path": "plan/2026-07-03-x.md", "spec_path": "docs/2026-07-03-x-spec.md"}
    )
    assert "## Source of truth (READ FIRST)" in section
    assert "Read plan/2026-07-03-x.md IN FULL" in section
    assert "Consult docs/2026-07-03-x-spec.md when intent is unclear" in section
    assert section.index("plan/2026-07-03-x.md") < section.index("docs/2026-07-03-x-spec.md")


def test_source_of_truth_section_plan_only():
    section = build_source_of_truth_section({"plan_path": "plan/2026-07-03-x.md", "spec_path": ""})
    assert "Read plan/2026-07-03-x.md IN FULL" in section
    assert "Consult" not in section


def test_source_of_truth_section_omitted_when_neither_path():
    assert build_source_of_truth_section({"plan_path": "", "spec_path": ""}) == ""
    assert build_source_of_truth_section(None) == ""
    assert build_source_of_truth_section({}) == ""
