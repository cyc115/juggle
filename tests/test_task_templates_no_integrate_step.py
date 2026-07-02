"""Coder/planner dispatch prompts must not tell the agent to run `juggle
integrate` directly — integrate is watchdog-owned (Task 10). This is the other
half of the migration: closing the compat window by no longer DISPATCHING the
old instruction, even though --allow-legacy-agent-integrate still exists as a
stopgap for already-running agents (Task 10)."""
import re
from pathlib import Path

from juggle_task_templates import TASK_TEMPLATES

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATE_PATTERN = re.compile(r"juggle integrate|juggle_cli\.py integrate")


def test_coder_template_does_not_instruct_direct_integrate():
    assert not INTEGRATE_PATTERN.search(TASK_TEMPLATES["coder"])


def test_planner_template_does_not_instruct_direct_integrate():
    assert not INTEGRATE_PATTERN.search(TASK_TEMPLATES["planner"])


def test_coder_template_has_finalization_error_rule():
    coder = TASK_TEMPLATES["coder"]
    assert "agent fail" in coder
    assert "never" in coder.lower() and "retry" in coder.lower()


def test_dispatch_command_files_do_not_instruct_direct_integrate():
    offenders = []
    for md_file in (REPO_ROOT / "commands").glob("*.md"):
        text = md_file.read_text()
        if INTEGRATE_PATTERN.search(text):
            offenders.append(md_file.name)
    assert offenders == []
