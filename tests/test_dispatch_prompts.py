"""Pins the superpowers skill names invoked by Juggle's dispatch-prompt
commands against the set actually shipped by the installed Superpowers
plugin (Superpowers 6, verified 2026-07-02 against v6.1.0's ``skills/``
directory and blog.fsck.com/2026/06/15/Superpowers-6). Catches a renamed
or removed skill silently breaking a `superpowers:<name>` invocation in a
dispatch prompt.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# Superpowers 6.1.0 skill directory names (no renames affect these — the
# only v6 rename was "Claude Search Optimization" -> "Skill Discovery
# Optimization", which Juggle does not invoke).
VALID_SUPERPOWERS_SKILLS = {
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
}

DISPATCH_PROMPT_FILES = [
    REPO_ROOT / "commands" / "start.md",
    REPO_ROOT / "commands" / "delegate.md",
]

SKILL_REF_RE = re.compile(r"superpowers:([a-z][a-z-]*[a-z])")


def _referenced_skills(text: str) -> set[str]:
    return set(SKILL_REF_RE.findall(text))


def test_dispatch_prompt_files_exist():
    for path in DISPATCH_PROMPT_FILES:
        assert path.is_file(), f"expected dispatch prompt file at {path}"


def test_dispatch_prompts_reference_only_valid_superpowers_skills():
    for path in DISPATCH_PROMPT_FILES:
        referenced = _referenced_skills(path.read_text())
        unknown = referenced - VALID_SUPERPOWERS_SKILLS
        assert not unknown, (
            f"{path} references superpowers skill(s) not in the installed "
            f"Superpowers 6 skill set: {sorted(unknown)}"
        )


def test_dispatch_prompts_reference_at_least_one_superpowers_skill():
    all_referenced: set[str] = set()
    for path in DISPATCH_PROMPT_FILES:
        all_referenced |= _referenced_skills(path.read_text())
    assert all_referenced, "expected dispatch prompts to invoke superpowers skills"


# ── PC2 (pc2-coder-cutover): coder dispatch prompt cutover hygiene pins ───────
# Exercises the REAL send_task_to_agent path end to end (not the pure
# render_agent_prompt unit under tests/test_prompt_render.py) so a
# regression in the dispatch-time wiring (thread label, repo profile, VCS
# resolution) is caught even if the pure renderer stays correct.


def _dispatch_prompt(tmp_path, monkeypatch, prompt, role="coder"):
    import subprocess
    import sys as _sys
    from unittest.mock import MagicMock

    _sys.path.insert(0, str(REPO_ROOT / "src"))
    from juggle_db import JuggleDB

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=repo, check=True)

    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir()
    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))

    db = JuggleDB(db_path=str(tmp_path / "d.db"))
    db.init_db()
    db.set_active(True)
    thread_id = db.create_thread("t1", session_id="")
    agent_id = db.create_agent(role, "%fake", repo_path=str(repo))

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.send_task.return_value = "mock_hash"
    mgr.run_task_oneshot.return_value = ("mock_hash", None)

    _core.send_task_to_agent(db, agent_id, thread_id, prompt, _mgr=mgr)
    return db.get_agent(agent_id)["last_task"]


def _dispatch_coder_prompt(tmp_path, monkeypatch, prompt):
    return _dispatch_prompt(tmp_path, monkeypatch, prompt, role="coder")


def test_coder_dispatch_prompt_states_each_previously_duplicated_rule_once(tmp_path, monkeypatch):
    """Spec problem #6: finalize/full-suite/pre-existing/watchdog rules were
    stated 2-4x across UNIVERSAL_PREAMBLE + TASK_TEMPLATES + the old topic
    Finish section. The real dispatched prompt must state each exactly once."""
    full_prompt = _dispatch_coder_prompt(tmp_path, monkeypatch, "Do the thing.")
    assert full_prompt.count("never stop at the prompt") == 1
    assert full_prompt.count("watchdog-owned") == 1
    assert full_prompt.count("Pre-existing test failures") == 1
    assert "## Universal rules (enforced for every agent)" not in full_prompt


def test_coder_dispatch_prompt_finalize_command_literal_and_singular(tmp_path, monkeypatch):
    full_prompt = _dispatch_coder_prompt(tmp_path, monkeypatch, "Do the thing.")
    assert full_prompt.count('agent complete') == 1
    assert "complete-agent" not in full_prompt
    assert "fail-agent" not in full_prompt
    assert "<thread>" not in full_prompt and "$THREAD" not in full_prompt


# ── PC3 (pc3-emitters-sweep): planner/researcher dispatch cutover ─────────────


@pytest.mark.parametrize("role", ["planner", "researcher"])
def test_planner_researcher_dispatch_prompt_states_each_rule_once(tmp_path, monkeypatch, role):
    full_prompt = _dispatch_prompt(tmp_path, monkeypatch, "Do the thing.", role=role)
    assert full_prompt.count("never stop at the prompt") == 1
    assert full_prompt.count("agent complete") == 1
    assert "## Universal rules (enforced for every agent)" not in full_prompt


def test_planner_dispatch_prompt_finalize_command_literal_and_singular(tmp_path, monkeypatch):
    full_prompt = _dispatch_prompt(tmp_path, monkeypatch, "Do the thing.", role="planner")
    assert full_prompt.count("agent complete") == 1
    assert "--open-questions" in full_prompt
    assert "complete-agent" not in full_prompt
    assert "fail-agent" not in full_prompt
    assert "<thread>" not in full_prompt and "$THREAD" not in full_prompt


def test_researcher_dispatch_prompt_finalize_command_literal_and_singular(tmp_path, monkeypatch):
    full_prompt = _dispatch_prompt(tmp_path, monkeypatch, "Do the thing.", role="researcher")
    assert full_prompt.count("agent complete") == 1
    assert "--retain" in full_prompt
    assert "complete-agent" not in full_prompt
    assert "fail-agent" not in full_prompt
    assert "<thread>" not in full_prompt and "$THREAD" not in full_prompt
