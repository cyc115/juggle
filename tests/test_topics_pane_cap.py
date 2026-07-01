"""Topics-pane cap — pure truncation layer over the ordered topic list.

Pins the '2026-06-30 topics pane cap' feature: the cockpit Topics pane shows AT
MOST N topics (N default 30; config cockpit.max_topics / env
JUGGLE_COCKPIT_MAX_TOPICS). Truncation is applied AFTER the existing group+sort:
non-terminal topics are NEVER dropped; terminal (done/closed/archived) topics are
dropped OLDEST-FIRST until the total fits N. Silent — no '+N more' marker.
"""
from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_model import Topic  # noqa: E402
from juggle_cockpit_topic_cap import cap_topics, resolve_max_topics  # noqa: E402


def _t(tid, status, age, *, is_current=False, label=None, project="INBOX"):
    return Topic(
        id=tid,
        label=label or tid,
        status=status,
        age_secs=age,
        is_current=is_current,
        project_id=project,
    )


# ── truth table ───────────────────────────────────────────────────────────────


def test_under_cap_returns_all_unchanged():
    """<= N topics → identity (no drops, order preserved)."""
    ts = [_t("a", "running", 1), _t("b", "closed", 2), _t("c", "active", 3)]
    assert cap_topics(ts, 30) == ts


def test_all_non_terminal_kept_even_over_cap():
    """(a) NEVER drop non-terminal topics — total may exceed N if it must."""
    ts = [_t(f"n{i}", "running", i) for i in range(5)]
    out = cap_topics(ts, 2)
    assert out == ts  # all 5 running kept despite N=2


def test_terminal_dropped_oldest_first():
    """(b) terminal topics truncated OLDEST-first (largest age_secs goes first)."""
    ts = [
        _t("live", "running", 1),
        _t("young", "closed", 10),
        _t("mid", "closed", 20),
        _t("old", "closed", 30),
    ]
    out = cap_topics(ts, 2)  # keep 2 → drop 2 oldest terminal (old, mid)
    ids = [t.id for t in out]
    assert "live" in ids and "young" in ids
    assert "old" not in ids and "mid" not in ids
    assert len(out) == 2


def test_total_never_exceeds_n_when_enough_terminal():
    """(c) total <= N when there are enough terminal topics to drop."""
    ts = [_t("live", "running", 0)] + [_t(f"c{i}", "closed", i + 1) for i in range(10)]
    out = cap_topics(ts, 4)
    assert len(out) == 4
    assert any(t.id == "live" for t in out)  # non-terminal survives


def test_survivor_order_preserved():
    """Truncation preserves the input order of survivors."""
    ts = [
        _t("a", "active", 5),
        _t("k1", "closed", 100),
        _t("b", "running", 6),
        _t("k2", "closed", 50),
    ]
    out = cap_topics(ts, 3)  # drop 1 oldest terminal → k1
    assert [t.id for t in out] == ["a", "b", "k2"]


def test_current_topic_never_dropped():
    """The current thread is protected even if its status looks terminal."""
    ts = [_t("cur", "closed", 999, is_current=True)] + [
        _t(f"c{i}", "closed", i) for i in range(5)
    ]
    out = cap_topics(ts, 2)
    assert any(t.is_current for t in out)


def test_busy_agent_topic_protected():
    """A topic with a live/busy agent (protected_labels) is never dropped."""
    ts = [
        _t("busy", "closed", 500, label="BZ"),
        _t("old", "closed", 400),
        _t("older", "closed", 600),
    ]
    out = cap_topics(ts, 1, protected_labels=frozenset({"BZ"}))
    assert any(t.label == "BZ" for t in out)


def test_archived_terminal_dropped_before_smaller_age():
    """Archived counts as terminal and is dropped oldest-first with the rest."""
    ts = [
        _t("run", "running", 1),
        _t("arch", "archived", 900),
        _t("done", "closed", 100),
    ]
    out = cap_topics(ts, 2)
    ids = [t.id for t in out]
    assert "run" in ids and "done" in ids and "arch" not in ids


def test_zero_or_negative_n_is_noop():
    """N<=0 is treated as 'no cap' (never hide the whole pane)."""
    ts = [_t("a", "closed", 1), _t("b", "closed", 2)]
    assert cap_topics(ts, 0) == ts
    assert cap_topics(ts, -1) == ts


# ── (d) N respects config / env ───────────────────────────────────────────────


def test_resolve_max_topics_default():
    """Default N is 30 when neither env nor config is set."""
    assert resolve_max_topics(env={}, config={}) == 30


def test_resolve_max_topics_from_config():
    """config cockpit.max_topics drives N."""
    assert resolve_max_topics(env={}, config={"cockpit": {"max_topics": 12}}) == 12


def test_resolve_max_topics_env_overrides_config():
    """env JUGGLE_COCKPIT_MAX_TOPICS wins over config."""
    assert (
        resolve_max_topics(
            env={"JUGGLE_COCKPIT_MAX_TOPICS": "7"},
            config={"cockpit": {"max_topics": 12}},
        )
        == 7
    )


def test_resolve_max_topics_invalid_falls_back():
    """Non-int env/config values fall back to the default, never crash."""
    assert resolve_max_topics(env={"JUGGLE_COCKPIT_MAX_TOPICS": "nope"}, config={}) == 30
    assert resolve_max_topics(env={}, config={"cockpit": {"max_topics": "x"}}) == 30


def test_resolve_max_topics_reads_disk_config(tmp_path, monkeypatch):
    """Real path: reads env + ~/.juggle/config.json (via _JUGGLE_CONFIG_PATH)."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"cockpit": {"max_topics": 15}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("JUGGLE_COCKPIT_MAX_TOPICS", raising=False)
    assert resolve_max_topics() == 15
    monkeypatch.setenv("JUGGLE_COCKPIT_MAX_TOPICS", "9")
    assert resolve_max_topics() == 9
