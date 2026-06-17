"""Regression tests for unguarded self.dismiss() in cockpit modals.

2026-06-17: _TailModal (and siblings) crash with ScreenStackError when
on_key fires after the modal is no longer the top screen (key-repeat,
double key event, or stray key).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")


class _DummyCapture:
    def __call__(self, pane: str, lines: int = 0) -> str:
        return "line1\nline2"


def _make_key(name: str):
    from textual.events import Key
    return Key(name, character=name if len(name) == 1 else None)


# ---------------------------------------------------------------------------
# Cycle 1 — _TailModal double-dismiss must not raise ScreenStackError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tail_modal_double_key_no_crash():
    """on_key twice on _TailModal must not raise ScreenStackError.

    2026-06-17: 'Can't pop screen; must have ≥1' on key-repeat / stray event.
    """
    from textual.app import App
    from textual.widgets import Label

    from juggle_cockpit_modals import _TailModal

    class _BaseApp(App):
        def compose(self):
            yield Label("base")

    async with _BaseApp().run_test() as pilot:
        modal = _TailModal("fake-pane", _DummyCapture())
        await pilot.app.push_screen(modal)
        await pilot.pause(0.05)
        # Pop so modal is no longer current
        await pilot.app.pop_screen()
        await pilot.pause(0.05)
        # Stray on_key while not current — must not raise
        modal.on_key(_make_key("q"))


# ---------------------------------------------------------------------------
# Cycle 2 — _PromptModal double-dismiss must not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_modal_double_dismiss_no_crash():
    """_PromptModal escape on_key while not current must not raise.

    2026-06-17: same unguarded dismiss pattern as _TailModal.
    """
    from textual.app import App
    from textual.widgets import Label

    from juggle_cockpit_modals import _PromptModal

    class _BaseApp(App):
        def compose(self):
            yield Label("base")

    async with _BaseApp().run_test() as pilot:
        modal = _PromptModal("Enter label:")
        await pilot.app.push_screen(modal)
        await pilot.pause(0.05)
        await pilot.app.pop_screen()
        await pilot.pause(0.05)
        modal.on_key(_make_key("escape"))  # must not raise


# ---------------------------------------------------------------------------
# Cycle 3 — _ConfirmModal double-dismiss must not raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_modal_double_dismiss_no_crash():
    """_ConfirmModal y/n while not current must not raise ScreenStackError.

    2026-06-17: same unguarded dismiss pattern.
    """
    from textual.app import App
    from textual.widgets import Label

    from juggle_cockpit_modals import _ConfirmModal

    class _BaseApp(App):
        def compose(self):
            yield Label("base")

    async with _BaseApp().run_test() as pilot:
        modal = _ConfirmModal("Are you sure?")
        await pilot.app.push_screen(modal)
        await pilot.pause(0.05)
        await pilot.app.pop_screen()
        await pilot.pause(0.05)
        modal.on_key(_make_key("y"))  # must not raise
