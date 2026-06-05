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


def test_build_match_profile_prompt_bounded_thread_count():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [{"topic": f"thread {i}", "assigned_by": "human"} for i in range(50)]
    prompt = build_match_profile_prompt(project, threads, [])
    assert prompt.count("thread ") <= 32
