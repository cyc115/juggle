"""juggle brief — pure human renderers (2026-07-07 juggle-brief-command).

Renderers are pure fns of a collected state dict (no DB/tmux/git), so budget and
content invariants are asserted on synthetic state. The session-snapshot budget
(≤~1500 chars) and the NO-archived-topics rule are the load-bearing contracts.
"""
from juggle_brief_render import (
    render_project_human,
    render_session_human,
    render_topic_human,
)

SESSION = {
    "version": "1.122.2",
    "watchdog": {"alive": True, "pid": 4242},
    "autopilot": True,
    "unconsumed_notifs": 2,
    "topics": [
        {"id": "SS", "state": "background", "state_badge": "🏃",
         "label": "scan-refactor", "title": "Refactor the scheduler scan",
         "open_questions": 1,
         "agent": {"role": "coder", "status": "busy"}},
        {"id": "SO", "state": "open", "state_badge": "",
         "label": "brief-cmd", "title": "Build juggle brief",
         "open_questions": 0, "agent": None},
        # Archived topic MUST be filtered by the renderer even if handed one.
        {"id": "ZZ", "state": "archived", "state_badge": "🗄️",
         "label": "ancient-topic-do-not-show", "title": "Ancient archived topic",
         "open_questions": 0, "agent": None},
    ],
    "agents": {
        "busy": 1, "idle": 3,
        "busy_list": [
            {"id": "ab12cd34", "role": "coder", "topic": "SS", "age": "3m",
             "reconciled": "busy"},
        ],
    },
    "actions": [
        {"id": 7, "priority": "high", "message": "Resolve integrate conflict on SO"},
    ],
    "health": {
        "git_branch": "main", "dirty": False,
        "worktrees": {"clean": 1, "unmerged": 2, "orphan": 0},
        "selfheal": {"count": 5, "top_sigs": ["ab12: KeyError in tick"]},
    },
}


def test_session_snapshot_under_budget():
    out = render_session_human(SESSION)
    assert len(out) <= 1500, f"snapshot is {len(out)} chars (budget 1500)"


def test_session_snapshot_excludes_archived():
    out = render_session_human(SESSION)
    assert "ancient-topic-do-not-show" not in out
    assert "Ancient archived topic" not in out
    assert "🗄️" not in out


def test_session_snapshot_has_active_topics_and_header():
    out = render_session_human(SESSION)
    assert "1.122.2" in out           # version
    assert "SS" in out and "SO" in out  # active topic ids
    assert "coder" in out             # busy agent role


def test_topic_render_shows_verdict_and_agent():
    ts = {
        "id": "SS", "state": "background", "title": "Refactor the scheduler scan",
        "open_questions": ["Which seam to split on?"],
        "agent": {"id": "ab12cd34", "role": "coder", "status": "busy",
                  "age": "3m", "last_line": "running pytest", "idle_at_prompt": False},
        "actions": [{"id": 7, "priority": "high", "message": "conflict"}],
        "notifications": [{"message": "agent dispatched", "created_at": "2026-07-07 09:00"}],
        "worktree": {"branch": "cyc_SS", "exists": True, "dirty": False,
                     "unmerged_commits": 2, "tests_green": None},
        "verdict": "unmerged-work",
    }
    out = render_topic_human(ts)
    assert "SS" in out
    assert "unmerged-work" in out      # the computed verdict is surfaced
    assert "coder" in out
    assert "cyc_SS" in out


def test_project_render_shows_rollup_and_failing():
    ps = {
        "id": "P4", "name": "Cockpit redesign", "armed": True,
        "counts": {"total": 14, "verified": 3, "failed": 1, "blocked": 0,
                   "ready": 2, "running": 1, "open": 7},
        "next_node": {"id": "T5", "title": "Wire the frontier rail"},
        "failing_nodes": ["T2", "T9"],
    }
    out = render_project_human(ps)
    assert "P4" in out
    assert "3/14" in out              # done rollup
    assert "T5" in out               # next actionable node
    assert "T2" in out and "T9" in out  # failing node ids
