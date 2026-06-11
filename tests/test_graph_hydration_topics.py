"""Topic hydration (R9): objective + dep-TOPIC handoffs + SEQUENTIAL task list
+ the per-task mark-task contract. Never thread.summary (DA M4)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_hydration import build_topic_hydration  # noqa: E402


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
    text = build_topic_hydration(
        "Proj objective", _topic(),
        deps=[{"id": "db", "title": "DB", "handoff": "schema v1", "diffstat": None}],
        tasks=_tasks(),
    )
    assert "Proj objective" in text and "Login e2e." in text
    assert "schema v1" in text                       # dep TOPIC handoff
    assert text.index("t1") < text.index("t2")       # sequential order preserved
    assert "mark-task" in text                       # per-task completion contract
    assert "complete-agent" in text                  # topic-level finish


def test_verified_task_flagged_for_skip():
    text = build_topic_hydration("", _topic(), deps=[], tasks=_tasks())
    assert "VERIFIED — skip" in text and "t2" in text
