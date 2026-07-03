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
