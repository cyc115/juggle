"""juggle brief — blocker-diagnosis verdict table (2026-07-07 juggle-brief-command).

diagnose(topic_state) is a PURE fn: synthetic state dicts in, one verdict out.
Every verdict branch gets a crafted row so a regression on any branch fails RED.
"""
from juggle_brief_diagnose import VERDICTS, diagnose


def test_failed_by_state():
    assert diagnose({"state": "failed-exec"}) == "failed"


def test_failed_by_failure_action():
    # A background topic with an open failure action item reads as failed even
    # though its node state is not (yet) a failure terminal.
    assert diagnose({"state": "background", "has_failure_action": True}) == "failed"


def test_dep_wait():
    assert diagnose({"state": "open", "deps_unmet": True}) == "dep-wait"


def test_no_agent():
    # Background (an agent should own it) but none is bound → orphaned.
    assert diagnose({"state": "background", "agent": None}) == "no-agent"


def test_idle_done_unreleased():
    ts = {
        "state": "background",
        "agent": {"idle_at_prompt": True, "status": "busy"},
        "work_landed": True,
    }
    assert diagnose(ts) == "idle-done-unreleased"


def test_unmerged_work():
    ts = {"state": "done", "agent": None, "unmerged_commits": 3, "work_landed": False}
    assert diagnose(ts) == "unmerged-work"


def test_healthy_running():
    ts = {"state": "background", "agent": {"idle_at_prompt": False, "status": "busy"}}
    assert diagnose(ts) == "healthy-running"


def test_every_verdict_is_enumerated():
    # The six documented verdicts and no stragglers.
    assert VERDICTS == frozenset({
        "idle-done-unreleased", "dep-wait", "failed",
        "unmerged-work", "no-agent", "healthy-running",
    })


def test_landed_idle_beats_unmerged():
    # A landed topic idling at its prompt is idle-done-unreleased, not unmerged.
    ts = {
        "state": "done",
        "agent": {"idle_at_prompt": True},
        "work_landed": True,
        "unmerged_commits": 0,
    }
    assert diagnose(ts) == "idle-done-unreleased"


def test_default_fallback_is_healthy():
    # No agent, no work, plain open conversation → benign default.
    assert diagnose({"state": "open"}) == "healthy-running"
