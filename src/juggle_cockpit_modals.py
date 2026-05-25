"""Juggle Cockpit — Textual modal screens.

Extracted from juggle_cockpit.py for modularity.
All symbols are re-exported from juggle_cockpit for backward compatibility.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static


class _PromptModal(ModalScreen):
    """Generic one-line input modal. Dismisses with the stripped value or None.

    dismiss_empty_as: value returned when the Input is blank (default None).
    Set to "" in action_filter so blank submit clears the filter, while Esc
    still returns None meaning "keep existing filter unchanged".
    """

    DEFAULT_CSS = """
    _PromptModal {
        align: center middle;
    }
    _PromptModal > Vertical {
        width: 44;
        height: 6;
        border: round $accent;
        padding: 1 2;
    }
    """

    def __init__(self, prompt: str, dismiss_empty_as=None) -> None:
        super().__init__()
        self._prompt = prompt
        self._dismiss_empty_as = dismiss_empty_as

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._prompt)
            yield Input(placeholder="…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        val = event.value.strip()
        self.dismiss(val if val else self._dismiss_empty_as)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()  # prevent Esc from bubbling to CockpitApp.on_key
            self.dismiss(None)


class _ConfirmModal(ModalScreen):
    """Single-keypress y/N confirm gate.

    Dismisses True on 'y', False on 'n' or Escape. No Input widget —
    the user only presses a single key. Cannot be submitted accidentally.
    """

    DEFAULT_CSS = """
    _ConfirmModal {
        align: center middle;
    }
    _ConfirmModal > Vertical {
        width: 52;
        height: 6;
        border: round $warning;
        padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            yield Label("[dim]y — confirm    n / Esc — cancel[/dim]")

    def on_key(self, event: events.Key) -> None:
        if event.key == "y":
            self.dismiss(True)
        elif event.key in ("n", "escape"):
            self.dismiss(False)


class _HelpModal(ModalScreen):
    """Help overlay listing all bindings, generated from CockpitApp.BINDINGS."""

    from textual.binding import Binding

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    _HelpModal {
        align: center middle;
    }
    _HelpModal > Static {
        width: 50;
        border: round $accent;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        from juggle_cockpit import CockpitApp  # lazy: avoids circular at module load
        # De-duplicate aliased scroll keys: show one row per unique action name.
        seen_actions: set[str] = set()
        lines: list[str] = ["Keyboard Shortcuts", "─" * 34]
        for b in CockpitApp.BINDINGS:
            if not b.description:
                continue
            if b.action in seen_actions:
                continue
            seen_actions.add(b.action)
            lines.append(f"  {b.key:<14} {b.description}")
        lines += ["", "Esc / q — close"]
        yield Static("\n".join(lines))
