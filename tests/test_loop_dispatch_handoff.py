"""P4b part-2 pins — cross-topic vault handoff: run-dir injection + pointer-not-payload
(loop-entity V2 §1, 2026-07-05).

A loop whose steps span differing (role, delivery) topics passes a payload from an
upstream topic A to a downstream topic B. Learnings/handoffs are capped and worktrees
are isolated, so the payload goes to a shared store (the vault): A writes its payload
to its `loop_run_dir(...)` and records a POINTER in its handoff; B hydrates A's handoff
(existing mechanism) and opens the file. Pins:

  * dispatch injects the loop run dir for a LOOP node (the graph dispatch path);
  * a NON-loop node gets NO injection (prompt unchanged);
  * a two-topic loop passes a pointer B can resolve (end-to-end — needs part-1
    multi-topic instantiation).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_cmd_loop_create import create_loop_atomic  # noqa: E402
from dbops import db_topics  # noqa: E402
from juggle_cmd_agents_graph_topics import mark_graph_topic  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "loop-handoff.db"))
    d.init_db()
    return d


@pytest.fixture
def loop_base(tmp_path, monkeypatch):
    """Point the config-sourced loop-output root at a throwaway dir (never the real
    vault) — the base is resolved through get_loop_output_root at the dispatch
    boundary (§1.6), so patching the settings resolver is enough."""
    base = tmp_path / "loops"
    monkeypatch.setattr(
        "juggle_vault_paths.get_settings",
        lambda: {"paths": {"loop_output_dir": str(base)}},
    )
    return base


def _single_topic_template():
    return {"topics": [{
        "id": "digest", "title": "Digest", "objective": "obj", "delivery": "deliver",
        "tasks": [{"id": "t0", "title": "t0", "prompt": "do", "role": "researcher",
                   "model": "sonnet", "verify_cmd": None, "deps": []}],
    }]}


def _two_topic_template():
    return {"topics": [
        {"id": "research", "title": "Research", "objective": "gather facts",
         "delivery": "deliver", "deps": [],
         "tasks": [{"id": "gather", "title": "gather", "prompt": "research the topic",
                    "role": "researcher", "model": "sonnet", "verify_cmd": None,
                    "deps": []}]},
        {"id": "notify", "title": "Notify", "objective": "send the digest",
         "delivery": "deliver", "deps": ["research"],
         "tasks": [{"id": "send", "title": "send", "prompt": "send the digest",
                    "role": "coder", "model": "sonnet", "verify_cmd": None,
                    "deps": []}]},
    ]}


# ── dispatch injects the run dir for a loop node ────────────────────────────────
def test_dispatch_injects_run_dir_for_loop_node(db, loop_base, monkeypatch):
    """dispatch-injects-run-dir — the graph dispatch path (_dispatch_via_pool →
    dispatch_node) injects this loop node's run-output directory into the prompt so
    the upstream agent knows WHERE to write its handoff payload (spec §1.5). The
    injected path is loop_run_dir(config-base, loop_id, run_seq, topic_id). RED today:
    no vault path reaches dispatch."""
    captured = {}

    def _fake_dispatch_node(db_, thread_id, prompt, task, *, role=None, model=None):
        captured["prompt"] = prompt

    monkeypatch.setattr("juggle_dispatch_core.dispatch_node", _fake_dispatch_node)

    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")
    topic = db_topics.get_topic(db, r["topic_id"])

    from juggle_graph_dispatch import _dispatch_via_pool
    _dispatch_via_pool(db, "thread-x", "ORIGINAL PROMPT BODY", topic)

    prompt = captured["prompt"]
    assert "ORIGINAL PROMPT BODY" in prompt  # original is preserved, not replaced
    expected = str(loop_base / r["loop_id"] / "0" / f"{r['topic_id']}.md")
    assert expected in prompt, f"run dir {expected} must be injected into the prompt"
    assert "handoff" in prompt.lower()  # tells the agent to record a POINTER, not payload


def test_dispatch_injects_into_topic_payload_context(db, loop_base, monkeypatch):
    """dispatch-injects-run-dir (payload path) — the graph tick dispatches a topic with
    a ``TopicPromptPayload`` (NOT a str), whose narrative lives in ``.context``. The
    injection must prepend to ``.context`` (frozen-dataclass replace), never string-
    concat onto the payload object. Regression pin (2026-07-05): an early str+payload
    concat would TypeError in production while a str-prompt unit test stayed green."""
    from juggle_prompt_context import TaskSpec, TopicPromptPayload

    captured = {}

    def _fake_dispatch_node(db_, thread_id, prompt, task, *, role=None, model=None):
        captured["prompt"] = prompt

    monkeypatch.setattr("juggle_dispatch_core.dispatch_node", _fake_dispatch_node)

    r = create_loop_atomic(db, template=_single_topic_template(), cadence="every 1h")
    topic = db_topics.get_topic(db, r["topic_id"])
    payload = TopicPromptPayload(
        context="ORIGINAL CONTEXT",
        tasks=(TaskSpec(id="t0", title="t0", verify_cmd="", body="do"),),
    )

    from juggle_graph_dispatch import _dispatch_via_pool
    _dispatch_via_pool(db, "thread-z", payload, topic)

    out = captured["prompt"]
    assert isinstance(out, TopicPromptPayload), "payload must stay a payload, not a str"
    assert out.tasks == payload.tasks  # tasks untouched
    expected = str(loop_base / r["loop_id"] / "0" / f"{r['topic_id']}.md")
    assert expected in out.context and "ORIGINAL CONTEXT" in out.context


def test_non_loop_node_gets_no_injection(db, loop_base, monkeypatch):
    """non-loop-no-injection — a normal (non-loop) topic dispatched through the SAME
    path gets NO run-dir injection; its prompt is passed through unchanged. The
    handoff wiring is scoped strictly to kind='loop' project nodes (§1.5)."""
    captured = {}

    def _fake_dispatch_node(db_, thread_id, prompt, task, *, role=None, model=None):
        captured["prompt"] = prompt

    monkeypatch.setattr("juggle_dispatch_core.dispatch_node", _fake_dispatch_node)

    pid = db.create_project(name="normal", objective="o")
    db_topics.create_topic(db, topic_id="T-plain", project_id=pid, title="Plain")
    topic = db_topics.get_topic(db, "T-plain")

    from juggle_graph_dispatch import _dispatch_via_pool
    _dispatch_via_pool(db, "thread-y", "PLAIN PROMPT", topic)

    assert captured["prompt"] == "PLAIN PROMPT", "a non-loop node must not be injected"


# ── end-to-end: a two-topic loop passes a pointer B can resolve ─────────────────
def test_two_topic_loop_passes_pointer_b_can_resolve(db, loop_base):
    """pointer-not-payload (end-to-end, needs part-1) — a two-topic loop's upstream
    topic A writes its payload to its loop_run_dir and records a POINTER in its
    handoff; the downstream topic B hydrates A's handoff (existing hydrate_for_topic)
    and can resolve + open the vault file. The PAYLOAD travels via the vault path; only
    a small POINTER travels in the handoff (spec §1)."""
    from juggle_loop_dispatch import loop_run_dir_for_node

    r = create_loop_atomic(db, template=_two_topic_template(), cadence="every 1h")
    loop_id = r["loop_id"]
    topic_a, topic_b = f"{loop_id}-research", f"{loop_id}-notify"
    pid = r["project_id"]

    # --- Upstream A's agent: write the payload to A's run dir, record a POINTER ---
    a_node = db_topics.get_topic(db, topic_a)
    run_dir = loop_run_dir_for_node(db, a_node)  # the SAME path dispatch injects
    assert run_dir is not None
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    payload = "# Research digest\n\nThree findings: X, Y, Z.\n"
    run_dir.write_text(payload, encoding="utf-8")
    pointer = f"Output written to {run_dir} — covers findings X/Y/Z."

    # A completes (deliver → 'delivered') with the POINTER as its handoff. Drive its
    # member task to a success terminal first so the topic can complete.
    with db._connect() as c:
        c.execute("UPDATE nodes SET state='delivered' WHERE kind='task' AND id=?",
                  (f"{loop_id}-r0-gather",))
        c.commit()
    thread_a = db.create_thread(topic="loop-A", session_id="s")
    db_topics.set_topic_thread(db, topic_a, thread_a)
    mark_graph_topic(db, thread_a, integrate_ok=True, handoff=pointer,
                     session_id="s", topic_id=topic_a)
    assert db_topics.get_topic(db, topic_a)["state"] == "delivered"

    # --- Downstream B hydrates A's handoff and resolves the pointer ---
    from juggle_graph_hydration import hydrate_for_topic
    b_node = db_topics.get_topic(db, topic_b)
    payload_obj = hydrate_for_topic(db, pid, b_node)
    context = payload_obj.context or ""

    assert str(run_dir) in context, "B's hydration must carry A's handoff POINTER"
    # pointer-not-payload: the ≤300-char handoff channel carries the path, not the file
    assert payload not in context, "the payload itself must NOT be inlined into hydration"
    # B can RESOLVE the pointer: the vault file exists and reads back the real payload
    assert run_dir.exists() and run_dir.read_text(encoding="utf-8") == payload
