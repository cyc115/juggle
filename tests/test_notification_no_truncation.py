"""Regression pin: the write seam never uses the old dumb truncation.

Incident 2026-06-16: long notifications were truncated to 280 chars with a
'…(full detail: get-messages <id>)' pointer suffix, hiding content inline. That
dumb render-time truncation must never come back.

Supersession 2026-07-05 (orchestrator scope update — concise cockpit item
formatter, layers 2+3): the DB write seam now SEMANTICALLY compresses an
OVER-BUDGET notification to the canonical 1-line ≤280-char budget via
juggle_item_compress. This pin is rewritten (not deleted) to protect the SAME
2026-06-16 core through the new seam — in-budget text is stored VERBATIM and the
old 'get-messages'/'full detail' truncation format never appears. (An action
item is bounded by LINES, not chars, so a long single-line action is still
stored verbatim — see test_add_action_item_stores_full_text below.)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


LONG_MESSAGE = "A" * 300 + " — important detail that must not be lost"


def test_add_notification_v2_over_budget_compressed_not_dumb_truncated(
    tmp_path, monkeypatch
):
    """add_notification_v2 compresses an over-budget message to the 1-line
    budget WITHOUT the legacy 'get-messages'/'full detail' pointer (2026-06-16
    core); a short message is still stored verbatim."""
    from juggle_db import JuggleDB

    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")
    with db._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO session (key, value) VALUES ('session_id', 'sess-x')"
        )
        conn.commit()

    db.add_notification_v2(tid, LONG_MESSAGE, "sess-x")
    msg = db.get_notifications_for_session("sess-x")[0]["message"]
    assert "\n" not in msg and len(msg) <= 280
    assert "full detail" not in msg and "get-messages" not in msg

    short = "merged @ ff9f051 — 4132 pass."
    db.add_notification_v2(tid, short, "sess-x")
    assert db.get_notifications_for_session("sess-x")[0]["message"] == short


def test_add_action_item_stores_full_text(tmp_path):
    """add_action_item must store the full message without truncation."""
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")

    db.add_action_item(tid, LONG_MESSAGE, "review")

    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == LONG_MESSAGE
    assert "full detail" not in items[0]["message"]
