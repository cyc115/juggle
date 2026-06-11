"""Hooks inject the FULL armed set with TOPIC-level status (R7/R9)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_hooks_autopilot as ha  # noqa: E402
import juggle_hooks_config as _cfg  # noqa: E402
from juggle_autopilot_state import ARMED_PROJECT_KEY  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "hooks.db"))
    d.init_db()
    monkeypatch.setattr(_cfg, "get_db", lambda: d)
    return d


def test_carveout_names_every_armed_project_and_addnode_route(db):
    """REGRESSION PIN (2026-06-10): the carve-out named ONE project — an agent
    could treat project 2's topics as manually dispatchable."""
    db.create_project(name="P1", objective="p1")
    db.create_project(name="P2", objective="p2")
    # get the actual ids
    projects = db.list_projects()
    p1 = next(p["id"] for p in projects if p["name"] == "P1")
    p2 = next(p["id"] for p in projects if p["name"] == "P2")
    tp.create_topic(db, topic_id="A1", project_id=p1, title="a")
    tp.create_topic(db, topic_id="B1", project_id=p2, title="b")
    db.set_setting(ARMED_PROJECT_KEY, f"{p1},{p2}")
    ctx = ha._armed_graph_context()
    first_line = ctx.splitlines()[0]
    assert p1 in first_line and p2 in first_line and "add-node" in ctx
    assert f"Graph [{p1}]" in ctx and f"Graph [{p2}]" in ctx


def test_injection_budget_split_keeps_total_bounded(db):
    db.create_project(name="P1", objective="p1")
    db.create_project(name="P2", objective="p2")
    db.create_project(name="P3", objective="p3")
    projects = db.list_projects()
    pids = [p["id"] for p in projects if p["name"] in ("P1", "P2", "P3")]
    for pid in pids:
        for i in range(8):
            tp.create_topic(db, topic_id=f"{pid}-t{i}", project_id=pid, title=f"t{i}")
    db.set_setting(ARMED_PROJECT_KEY, ",".join(pids))
    ctx = ha._armed_graph_context()
    lines = [l for l in ctx.splitlines() if l.startswith("Graph [")]
    assert len(lines) == 3 and sum(len(l) for l in lines) <= 540


def test_disarmed_returns_empty(db):
    assert ha._armed_graph_context() == ""
