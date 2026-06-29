"""Prose-decision auto-bridge: Stop-hook scanner files action items.

Incident 2026-06-29: a decision/advisory surfaced as plain assistant PROSE
(e.g. "... collides with rss-dashboard — your call") never reached the
cockpit action-items pane, because only AskUserQuestion auto-files an item.
handle_stop now mirrors that bridge for prose, and handle_user_prompt_submit
auto-acks it on the user's next reply.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def active_db(tmp_path, monkeypatch):
    # Use juggle.db so is_active()/get_db() (which read juggle_hooks_config.DB_PATH
    # via _db_path() at call time) resolve to this isolated file.
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Topic A", session_id="s1")
    db.set_current_thread(tid)

    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    import juggle_hooks_config
    import juggle_hooks

    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", db.db_path)
    return db


def _decision_items(db):
    return [
        i
        for i in db.get_open_action_items()
        if i.get("message", "").startswith("[auto-decision]")
    ]


# ---------------------------------------------------------------------------
# Criterion 6 (regression pin) + Criterion 1: prose decision -> action item
# ---------------------------------------------------------------------------


def test_prose_decision_surfaced_as_action_item_2026_06_29(active_db):
    """2026-06-29: prose decision ("... — your call") not surfaced as action item.

    RED on pre-fix code (handle_stop only emitted a notification nudge); GREEN
    after the Stop-hook scanner also files a type=decision action item.
    """
    import juggle_hooks

    msg = "mike-rss manifest id collides with rss-dashboard — your call"
    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({"last_assistant_message": msg})

    items = active_db.get_open_action_items()
    decisions = [i for i in items if i["type"] == "decision"]
    assert len(decisions) == 1, f"Expected one decision item, got: {items}"
    assert "collides with rss-dashboard" in decisions[0]["message"]


# ---------------------------------------------------------------------------
# Criterion 2: dedup — same message twice yields exactly one open item
# ---------------------------------------------------------------------------


def test_prose_decision_deduped_across_repeated_stops(active_db):
    import juggle_hooks

    msg = "Two viable options here — your call which one to take."
    for _ in range(2):
        with pytest.raises(SystemExit):
            juggle_hooks.handle_stop({"last_assistant_message": msg})

    assert len(_decision_items(active_db)) == 1


# ---------------------------------------------------------------------------
# Criterion 3: dedup vs AskUserQuestion — do not double-file
# ---------------------------------------------------------------------------


def test_prose_decision_skips_when_askuser_already_filed(active_db):
    import juggle_hooks

    tid = active_db.get_current_thread()
    active_db.add_action_item(
        thread_id=tid,
        message="[tuid:tuid-abc] Decision needed: Option A? / Option B?",
        type_="decision",
        priority="normal",
    )

    msg = "Option A or Option B — your call."
    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({"last_assistant_message": msg})

    # The AskUserQuestion item stands; no [auto-decision] duplicate is added.
    assert _decision_items(active_db) == []
    items = active_db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"].startswith("[tuid:")


# ---------------------------------------------------------------------------
# Criterion 4: negative — a clean completion files nothing
# ---------------------------------------------------------------------------


def test_clean_completion_files_no_action_item(active_db):
    import juggle_hooks

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({"last_assistant_message": "Done. Tests green."})

    assert active_db.get_open_action_items() == []


# ---------------------------------------------------------------------------
# Criterion 5: auto-ack — next user prompt dismisses the prose decision
# ---------------------------------------------------------------------------


def test_user_prompt_submit_auto_acks_prose_decision(active_db):
    import juggle_hooks

    tid = active_db.get_current_thread()
    active_db.add_action_item(
        thread_id=tid,
        message="[auto-decision] Two options — your call.",
        type_="decision",
        priority="normal",
    )
    assert len(_decision_items(active_db)) == 1

    with pytest.raises(SystemExit):
        juggle_hooks.handle_user_prompt_submit({"prompt": "go with option A"})

    assert _decision_items(active_db) == []
