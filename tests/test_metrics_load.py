"""Run load + slice/group (2026-06-30 orchestration-metrics Task 6)."""
import pytest

import juggle_metrics_load as ml


def test_group_runs_by_role():
    runs = [{"role": "coder"}, {"role": "planner"}, {"role": "coder"}]
    g = ml.group_runs(runs, "role")
    assert set(g) == {"coder", "planner"} and len(g["coder"]) == 2


def test_group_runs_none_is_all():
    assert list(ml.group_runs([{"role": "x"}], None)) == ["all"]


def test_group_runs_unknown_key_raises():
    with pytest.raises(ValueError):
        ml.group_runs([], "banana")


def test_load_runs_since_filter(juggle_db):
    tid = juggle_db.create_thread(topic="t", session_id="s")
    juggle_db.insert_agent_run(thread_id=tid, input_prompt="p", agent_id=None, role="coder",
                               model="m", harness="claude", project_id="INBOX",
                               topic_id=None, task_id="t1")
    assert len(ml.load_runs(juggle_db, since="2000-01-01T00:00:00")) == 1
    assert len(ml.load_runs(juggle_db, since="2999-01-01T00:00:00")) == 0
