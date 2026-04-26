"""Golden snapshot tests for cockpit view layer.

Run with UPDATE_SNAPSHOTS=1 to regenerate golden files.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import time as _time
from collections import namedtuple
from pathlib import Path

from rich.console import Console

from juggle_cockpit_model import Topic, Action, Agent, Notification, CockpitState
from juggle_cockpit_view import build_layout, render_into

SNAPSHOTS_DIR = Path(__file__).parent / "cockpit_snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"

Size = namedtuple("Size", ["width", "height"])


def _make_state():
    return CockpitState(
        topics=[
            Topic(id="t1", label="K", status="current",  age_secs=300,  is_current=True,  title="cockpit refactor"),
            Topic(id="t2", label="J", status="running",  age_secs=3600, is_current=False, title="talkback"),
            Topic(id="t3", label="B", status="paused",   age_secs=7200, is_current=False, title="orchestrator hygiene"),
        ],
        actions=[
            Action(id="a1", topic_id="K", text="approve plan v3",         tier=2, age_secs=300),
            Action(id="a2", topic_id="J", text="BLOCKER: missing token",  tier=0, age_secs=600),
        ],
        agents=[
            Agent(id_short="abcd1234", role="coder",   status="busy",  topic_id="K", age_secs=720),
            Agent(id_short="ef567890", role="planner", status="stale", topic_id="J", age_secs=10800),
        ],
        notifications=[
            Notification(text="plan v3 ready",    kind="complete", age_secs=30),
            Notification(text="agent timed out",  kind="warning",  age_secs=120),
        ],
        fetched_at=_time.time(),
    )


def _render_to_text(bp: str, width: int) -> str:
    state = _make_state()
    layout = build_layout(bp, topics_count=len(state.topics))
    render_into(layout, state, bp)
    c = Console(record=True, width=width, no_color=True)
    with c:
        c.print(layout)
    return c.export_text()


def _check_or_update(name: str, actual: str):
    path = SNAPSHOTS_DIR / f"{name}.txt"
    if UPDATE:
        path.write_text(actual)
        return
    if not path.exists():
        raise AssertionError(
            f"Snapshot {path} missing. Run: UPDATE_SNAPSHOTS=1 pytest tests/test_cockpit_snapshots.py"
        )
    expected = path.read_text()
    assert actual == expected, (
        f"Snapshot {name} changed. Run UPDATE_SNAPSHOTS=1 to accept new output.\n"
        f"--- expected (first diff line) ---\n"
        + "\n".join(
            f"  L{i+1}: {e!r} → {a!r}"
            for i, (e, a) in enumerate(zip(expected.splitlines(), actual.splitlines()))
            if e != a
        )[:500]
    )


def test_snapshot_wide():
    text = _render_to_text("wide", 140)
    _check_or_update("wide_140", text)


def test_snapshot_medium():
    text = _render_to_text("medium", 100)
    _check_or_update("medium_100", text)


def test_snapshot_narrow():
    text = _render_to_text("narrow", 70)
    _check_or_update("narrow_70", text)
