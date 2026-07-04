"""Pins the learnings-rollup triage documentation in commands/start.md
(spec 2026-07-03 §6 + Final revisions). The orchestrator's triage ladder is
codified in start.md — not prompt memory — so the monitor's ``learnings_rollup``
event has a documented handling procedure with the four dispositions (a/b/c/d).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
START_MD = REPO_ROOT / "commands" / "start.md"


def _text() -> str:
    return START_MD.read_text()


def test_start_md_documents_learnings_rollup_monitor_line():
    text = _text()
    # The rollup event surfaces through the monitor as a pushable line.
    assert "learnings_rollup" in text or "learnings rollup" in text


def test_start_md_has_triage_section_with_four_dispositions():
    text = _text().lower().replace("`", "")
    # (a) feature seed → brainstorm → plan → graph add-task
    assert "brainstorm" in text and "graph add-task" in text
    # (b) project convention → repo CLAUDE.md
    assert "repo claude.md" in text
    # (c) globally applicable → global CLAUDE.md
    assert "global claude.md" in text or "~/.claude/claude.md" in text
    # (d) too specific → leave in DB
    assert "leave" in text and ("in db" in text or "in the db" in text)


def test_start_md_triage_ladder_is_per_learning():
    text = _text()
    # Judgment is per-learning, orchestrator-owned (spec §6).
    assert "learnings_rollup" in text
    assert "graph learnings" in text
