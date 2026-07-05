"""P4a pins: role-only multi-topic loop-template validator + pre-create confirm-card
(loop-entity V2, 2026-07-05).

§6: partition topics on ``(role, delivery)`` — **model is NOT a partition key**. The
validator now (a) permits N topics; (b) rejects a topic whose member tasks disagree
on ``(role, delivery)``; (c) requires a UNIFORM model per topic (one topic = one
pane = one model, §2.2); (d) validates the cross-topic dep-DAG is acyclic and every
edge connects DISTINCT topics. §6.3: ``schedule:create`` renders a deterministic
confirm-card of the decomposed topic-DAG before the (frozen-forever) create — code,
not prompt.

RED framing: several pins are cast as MULTI-topic templates precisely so they fail on
the pre-change V1 code, which raised the single-topic guard ("got N topics …") before
ever reaching the mixing / model / DAG checks.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_loop_template_validator import (  # noqa: E402
    LoopTemplateError,
    validate_loop_template,
)


def _task(tid, role, *, delivery=None, model=None, deps=None):
    t = {"id": tid, "title": tid, "prompt": f"do {tid}", "role": role,
         "verify_cmd": None, "deps": deps or []}
    if delivery is not None:
        t["delivery"] = delivery
    if model is not None:
        t["model"] = model
    return t


# ── Multi-topic partition on (role, delivery) ───────────────────────────────────
def test_two_topic_differing_role_validates():
    """P4a §6 (2026-07-05): a 2-topic template whose topics differ in role — a
    researcher 'deliver' topic feeding a coder 'merge' topic — validates. The V1
    single-topic guard is lifted; the partition key is (role, delivery)."""
    tmpl = {"topics": [
        {"id": "research", "title": "Research", "delivery": "deliver", "deps": [],
         "tasks": [_task("gather", "researcher", delivery="deliver")]},
        {"id": "build", "title": "Build", "delivery": "merge", "deps": ["research"],
         "tasks": [_task("impl", "coder", delivery="merge")]},
    ]}
    norm = validate_loop_template(tmpl)
    assert [t["id"] for t in norm["topics"]] == ["research", "build"]
    assert norm["topics"][0]["role"] == "researcher"
    assert norm["topics"][0]["delivery"] == "deliver"
    assert norm["topics"][1]["role"] == "coder"
    assert norm["topics"][1]["deps"] == ["research"]


def test_mixed_role_delivery_within_topic_rejected():
    """P4a §6: a topic whose member tasks disagree on (role, delivery) is rejected in
    CODE, never trusted from the LLM. Framed multi-topic so it is RED on the V1 guard
    (which raised 'single-topic', not the (role, delivery) mixing error)."""
    tmpl = {"topics": [
        {"id": "ok", "title": "OK", "delivery": "merge", "deps": [],
         "tasks": [_task("x", "coder", delivery="merge")]},
        {"id": "bad", "title": "Bad", "delivery": "merge", "deps": [],
         "tasks": [_task("a", "researcher", delivery="merge"),
                   _task("b", "coder", delivery="merge")]},
    ]}
    with pytest.raises(LoopTemplateError, match="role"):
        validate_loop_template(tmpl)


def test_mixed_model_topic_rejected():
    """P4a §6: model is NOT a partition key, but a topic must carry ONE model (one
    topic = one pane = one model, §2.2). A topic pinning two DIFFERENT non-null models
    is a template error. Framed multi-topic so it is RED on the V1 single-topic guard."""
    tmpl = {"topics": [
        {"id": "ok", "title": "OK", "delivery": "merge", "deps": [],
         "tasks": [_task("x", "coder", delivery="merge")]},
        {"id": "bad", "title": "Bad", "delivery": "merge", "deps": [],
         "tasks": [_task("a", "coder", delivery="merge", model="sonnet"),
                   _task("b", "coder", delivery="merge", model="opus")]},
    ]}
    with pytest.raises(LoopTemplateError, match="model"):
        validate_loop_template(tmpl)


def test_same_role_delivery_different_model_collapses_to_one_topic():
    """P4a §6: model is NOT a partition key — same-(role, delivery) tasks that differ
    only in desired model do NOT force a topic split; they collapse into ONE topic
    sharing that topic's single model (the pinned one wins; an unset task inherits it).
    RED: V1's (role, model, delivery) signature rejected this as a mixed topic."""
    tmpl = {"topics": [
        {"id": "digest", "title": "Digest", "delivery": "deliver", "deps": [],
         "tasks": [
             _task("gather", "researcher", delivery="deliver", model="sonnet"),
             _task("write", "researcher", delivery="deliver"),  # model unset
         ]},
    ]}
    norm = validate_loop_template(tmpl)
    assert len(norm["topics"]) == 1                       # no forced split
    topic = norm["topics"][0]
    assert topic["model"] == "sonnet"                     # the pinned model wins
    assert all(tk["model"] == "sonnet" for tk in topic["tasks"])  # unset inherits


# ── Cross-topic dep-DAG ─────────────────────────────────────────────────────────
def test_cyclic_and_self_edge_rejected():
    """P4a §6: the cross-topic dep-DAG must be ACYCLIC and every edge must connect
    DISTINCT topics. A self-edge and a 2-cycle are both rejected. RED: V1 ignored
    topic-level deps entirely (a single self-looping topic validated silently)."""
    self_edge = {"topics": [
        {"id": "a", "title": "A", "delivery": "merge", "deps": ["a"],
         "tasks": [_task("x", "coder", delivery="merge")]},
    ]}
    with pytest.raises(LoopTemplateError, match="itself|distinct"):
        validate_loop_template(self_edge)

    cycle = {"topics": [
        {"id": "a", "title": "A", "delivery": "merge", "deps": ["b"],
         "tasks": [_task("x", "coder", delivery="merge")]},
        {"id": "b", "title": "B", "delivery": "merge", "deps": ["a"],
         "tasks": [_task("y", "coder", delivery="merge")]},
    ]}
    with pytest.raises(LoopTemplateError, match="cycle"):
        validate_loop_template(cycle)


def test_cross_topic_edge_to_unknown_topic_rejected():
    """P4a §6: a cross-topic dep referencing a topic not in the template is rejected
    (a dangling topic edge would wedge the downstream generation at never-ready)."""
    tmpl = {"topics": [
        {"id": "a", "title": "A", "delivery": "merge", "deps": ["ghost"],
         "tasks": [_task("x", "coder", delivery="merge")]},
    ]}
    with pytest.raises(LoopTemplateError, match="not a topic"):
        validate_loop_template(tmpl)


# ── Pre-create confirm-card (§6.3) ──────────────────────────────────────────────
def test_confirm_card_renders_topic_dag():
    """P4a §6.3: schedule:create renders a deterministic confirm-card of the decomposed
    topic-DAG — topics · (role, delivery, model) · cross-topic edges · cadence — BEFORE
    the frozen-forever create. Code, not prompt. RED: the renderer did not exist."""
    from juggle_loop_confirm_card import render_topic_dag_card

    tmpl = {"topics": [
        {"id": "research", "title": "Research news", "delivery": "deliver", "deps": [],
         "tasks": [_task("gather", "researcher", delivery="deliver", model="sonnet")]},
        {"id": "notify", "title": "Send digest", "delivery": "merge", "deps": ["research"],
         "tasks": [_task("send", "coder", delivery="merge")]},
    ]}
    norm = validate_loop_template(tmpl)
    card = render_topic_dag_card(norm, "daily at 08:00")

    assert "daily at 08:00" in card                       # cadence
    assert "research" in card and "notify" in card        # both topics
    assert "researcher" in card and "coder" in card       # roles
    assert "deliver" in card and "merge" in card          # deliveries
    assert "sonnet" in card                               # pinned model
    assert "research → notify" in card                    # cross-topic edge


def test_schedule_create_wires_confirm_card_cli():
    """P4a §6.3 (code over prompts): schedule:create must invoke the code-backed
    `loop plan` renderer for its confirm-card, not hand-render a prompt-only card that
    can silently drift from the validator's decomposition."""
    doc = Path(__file__).resolve().parents[1] / "commands" / "schedule:create.md"
    text = doc.read_text(encoding="utf-8")
    assert "loop plan" in text
