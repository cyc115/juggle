"""Graph panel renders a numbered, multi-column topological task LIST."""
import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from juggle_cockpit_graph_layout import GraphTask
from juggle_cockpit_graph_panel import build_graph_panel, topological_order

IDS = ["task1", "schema-migration-37", "topic-store", "spec-format-topics",
       "fair-scheduler", "tick-dispatch-topics", "completion-marking",
       "cli-hooks-r8-guard", "cockpit-topic-tree", "full-gates-e2e-version"]


def _graph():
    states = ["running"] + ["open"] * 9
    tasks = [
        GraphTask(id=i, title=i, state=s,
                  thread_id=("w" if s == "running" else None),
                  user_label=("WK" if s == "running" else None))
        for i, s in zip(IDS, states)
    ]
    edges = [(IDS[k], IDS[k - 1]) for k in range(1, len(IDS))]
    return tasks, edges


def _render(width=120, height=10):
    tasks, edges = _graph()
    c = Console(record=True, width=width)
    c.print(build_graph_panel(project_id="P2", tasks=tasks, edges=edges,
                              selection=0, unread=0, width=width, height=height,
                              pan_offset=0))
    return c.export_text()


def test_topological_order_is_execution_order():
    tasks, edges = _graph()
    assert [n.id for n in topological_order(tasks, edges)] == IDS


def test_all_tasks_visible_at_wide_width():
    out = _render(120)
    for nid in IDS:
        stem = nid[:10]  # long ids may ellipsize the tail
        assert stem in out, f"task '{nid}' missing from list:\n{out}"


def test_tasks_are_numbered():
    out = _render(120)
    for n in ("1", "5", "10"):
        assert n in out


def test_running_task_shows_agent_label():
    out = _render(120)
    assert "[WK]" in out


def test_blocked_task_shows_dependency_marker():
    out = _render(120)
    assert "⊣1" in out  # task2 waits on task #1


def test_no_overflow_when_narrow():
    out = _render(40, 12)
    for line in out.splitlines():
        assert len(line) <= 40, f"overflow {len(line)}: {line!r}"
