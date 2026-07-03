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
