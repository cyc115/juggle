"""Regression pins for defect F (2026-07-01): thread-mirror dedup.

When the watchdog tick dispatches a graph task whose owning topic was created via
`add-task --topic <conversation>`, it must REUSE that existing descriptive
conversation as the surfacing/dispatch thread instead of spawning a second
"[T-<id>] ..." mirror row. Incident duplicate pairs: HY/IE, HZ/ID, IA/IJ; plus
IJ resurrection — an archived mirror regenerated on the very next tick.

Invariant pinned: ONE conversation row per task in the cockpit topics pane, ever.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402
from juggle_add_node import add_node  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "mirror.db"))
    d.init_db()
    return d


def _args(db, **kw):
    base = dict(
        project="INBOX", id="x", title="X", prompt="do x",
        deps=None, required_by=None, verify_cmd=None, json_out=False,
        topic=None, db_path=str(db.db_path),
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDispatch:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, db, thread_id, prompt, topic):
        self.calls.append((thread_id, topic["id"]))


def _conv_titles(db) -> list[str]:
    return [t["title"] for t in db.get_all_threads()]


def _force_ready(db, topic_id: str) -> None:
    """Bounce a topic back to 'ready' (simulates the reconcile reset that made
    the incident's archived mirror re-dispatchable)."""
    from dbops.schema import _now

    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET state='ready', updated_at=? WHERE id=? AND kind='topic'",
            (_now(), topic_id),
        )
        conn.commit()


# ── Pin 1: HY/IE — dispatch reuses the descriptive conversation, no mirror ──────


def test_dispatch_reuses_descriptive_conversation_no_mirror_row(db):
    # The descriptive conversation title deliberately does NOT lexically match the
    # task/topic title — reuse must ride the EXPLICIT add-task --topic mapping, not
    # the lexical dedup heuristic (which silently missed the real incident when the
    # human title differed / lived under another project).
    conv = add_node(
        db, kind="conversation", title="Nightly ops review",
        project_id="INBOX",
    )
    cg.cmd_graph_add_task(
        _args(db, id="fix-daemon-staleness",
              title="fix daemon staleness restart", topic=conv["node_id"])
    )
    assert len(db.get_all_threads()) == 1  # only the descriptive conversation

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert "T-fix-daemon-staleness" in stats["dispatched"]
    # No second "[T-*]" mirror conversation was spun up.
    assert len(db.get_all_threads()) == 1
    assert not any(str(t).startswith("[T-") for t in _conv_titles(db))
    # The descriptive conversation IS the surfacing/dispatch thread.
    assert tp.get_topic(db, "T-fix-daemon-staleness")["thread_id"] == conv["node_id"]
    assert fake.calls == [(conv["node_id"], "T-fix-daemon-staleness")]


# ── Pin 2: IJ — an archived surface is never resurrected as a fresh mirror ───────


def test_archived_surface_not_resurrected_on_next_tick(db):
    conv = add_node(
        db, kind="conversation", title="reuse before cap", project_id="INBOX",
    )
    cg.cmd_graph_add_task(
        _args(db, id="fix-reuse-before-cap", title="reuse before cap",
              topic=conv["node_id"])
    )
    tid = "T-fix-reuse-before-cap"

    fake = FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)
    assert len(db.get_all_threads()) == 1

    # Ops archives the surfacing conversation; the topic bounces back to ready.
    db.set_thread_status(conv["node_id"], "archived")
    _force_ready(db, tid)

    gd.graph_tick(db, dispatch_fn=FakeDispatch())

    # The very next tick must NOT regenerate a fresh "[T-*]" mirror row.
    assert len(db.get_all_threads()) == 1
    assert not any(str(t).startswith("[T-") for t in _conv_titles(db))
