"""REGRESSION (2026-07-02, user-reported "(G) doesn't work"): most terminals
deliver Shift+<letter> keypresses as the literal uppercase character event
(e.g. "G"), not a "shift+g" modified-key event — there is no shift+<letter>
escape sequence for plain letters on most terminals (unlike shift+tab,
shift+arrow, which do have distinct sequences). Binding("shift+g", ...) and
Binding("shift+c", ...) therefore never matched what the terminal actually
sent, so pressing Shift+G/Shift+C silently did nothing. Bindings must use the
literal uppercase key string.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _armed_db(tmp_path):
    from juggle_db import JuggleDB
    from dbops import db_graph as g

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Proj", "active", now, now),
        )
        conn.commit()
    g.create_task(db, task_id="n1", project_id="P", title="One", prompt="do 1")
    return db_path


def test_bindings_use_reachable_uppercase_key_strings():
    """Binding table must not rely on 'shift+<letter>' strings — Textual/most
    terminals never deliver that form for plain letters, so such bindings are
    permanently unreachable."""
    from juggle_cockpit import CockpitApp

    shift_letter_bindings = [
        b.key for b in CockpitApp.BINDINGS
        if b.key.startswith("shift+") and len(b.key) == len("shift+") + 1
        and b.key[-1].isalpha()
    ]
    assert shift_letter_bindings == []


def test_G_binding_present_and_lowercase_g_untouched():
    from juggle_cockpit import CockpitApp

    actions_by_key = {b.key: b.action for b in CockpitApp.BINDINGS}
    assert actions_by_key.get("G") == "graph_railroad"
    assert actions_by_key.get("g") == "toggle_graph"


def test_C_binding_present():
    from juggle_cockpit import CockpitApp

    actions_by_key = {b.key: b.action for b in CockpitApp.BINDINGS}
    assert actions_by_key.get("C") == "close"


@pytest.mark.asyncio
async def test_uppercase_G_key_opens_railroad_screen(tmp_path):
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_railroad import RailroadScreen

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("G")
        await pilot.pause(0.15)
        assert isinstance(app.screen, RailroadScreen)


@pytest.mark.asyncio
async def test_uppercase_C_key_triggers_close_prompt(tmp_path):
    from juggle_cockpit import CockpitApp
    from juggle_cockpit_modals import _PromptModal

    app = CockpitApp(db_path=_armed_db(tmp_path))
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.press("C")
        await pilot.pause(0.15)
        assert isinstance(app.screen, _PromptModal)
