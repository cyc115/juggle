import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_build_match_profile_prompt_contains_project_name():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI assistant platform"}
    threads = [
        {"topic": "fix agent dispatch bug", "assigned_by": "human"},
        {"topic": "add terraform module", "assigned_by": "human"},
        {"topic": "auto-assigned CI thread", "assigned_by": "auto"},
    ]
    corrections = [{"topic": "investing script", "from_project": "P1", "to_project": "P2"}]
    prompt = build_match_profile_prompt(project, threads, corrections)
    assert "LifeOS Dev" in prompt
    assert "fix agent dispatch bug" in prompt
    assert "auto-assigned CI thread" in prompt


def test_build_match_profile_prompt_human_weighted_before_auto():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [
        {"topic": "human thread", "assigned_by": "human"},
        {"topic": "auto thread", "assigned_by": "auto"},
    ]
    prompt = build_match_profile_prompt(project, threads, [])
    assert "human thread" in prompt


def test_build_match_profile_prompt_includes_negative_framing():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    prompt = build_match_profile_prompt(project, [], [])
    assert "NOT" in prompt or "negative" in prompt.lower() or "sibling" in prompt.lower()


from unittest.mock import patch, MagicMock


def test_synth_project_writes_match_profile(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build things")
    tid = db.create_thread("software task", session_id="s1")
    db.update_thread(tid, project_id=pid, assigned_by="human")
    with patch("juggle_cmd_projects.llm_call", return_value=(
        "Software development threads.\nKEYWORDS: code, deploy, CI\nNOT: finance, investing"
    )):
        synth_project(db, pid)
    p = db.get_project(pid)
    assert "Software development" in p["match_profile"]
    assert p["profile_dirty"] == 0
    assert p["profile_synth_at"] is not None


def test_synth_project_skips_if_no_threads_and_no_force(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Empty", "No threads yet")
    with patch("juggle_cmd_projects.llm_call") as mock_llm:
        synth_project(db, pid)
    mock_llm.assert_not_called()


def test_assign_marks_old_project_dirty(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import _assign_thread_to_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    p1 = db.create_project("P1", "obj1")
    p2 = db.create_project("P2", "obj2")
    tid = db.create_thread("some topic", session_id="s1")
    db.update_thread(tid, project_id=p1, assigned_by="human")
    _assign_thread_to_project(db, tid, p2, assigned_by="human")
    assert db.get_project(p1)["profile_dirty"] == 1


def test_build_match_profile_prompt_bounded_thread_count():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [{"topic": f"thread {i}", "assigned_by": "human"} for i in range(50)]
    prompt = build_match_profile_prompt(project, threads, [])
    assert prompt.count("thread ") <= 32
