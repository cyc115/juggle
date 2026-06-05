"""Juggle Cockpit — Textual drag-handle widgets.

Extracted from juggle_cockpit.py for modularity.
All symbols are re-exported from juggle_cockpit for backward compatibility.
"""

from __future__ import annotations

from textual import events
from textual.widgets import Static


class Splitter(Static):
    """Vertical drag handle. Resizes the left/right widget pair on either side."""

    DEFAULT_CSS = """
    Splitter {
        width: 1;
        height: 100%;
        background: $panel-darken-1;
        color: $panel-lighten-2;
    }
    Splitter:hover {
        background: $accent;
    }
    """

    def __init__(
        self,
        left_id: str,
        right_id: str,
        min_left_pct: int = 10,
        min_right_pct: int = 10,
    ) -> None:
        super().__init__("│")
        self._left_id = left_id
        self._right_id = right_id
        self._dragging = False
        self._drag_start_x: int = 0
        self._left_start_w: int = 0
        self._right_start_w: int = 0
        self._min_left_pct = min_left_pct
        self._min_right_pct = min_right_pct

    def on_mouse_down(self, event: events.MouseDown) -> None:
        left = self.app.query_one(f"#{self._left_id}")
        right = self.app.query_one(f"#{self._right_id}")
        self._dragging = True
        self._drag_start_x = event.screen_x
        self._left_start_w = left.size.width
        self._right_start_w = right.size.width
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = event.screen_x - self._drag_start_x
        total = self._left_start_w + self._right_start_w
        if total <= 0:
            return
        new_left_cells = self._left_start_w + delta
        new_left_pct = max(
            self._min_left_pct,
            min(100 - self._min_right_pct, int(new_left_cells / total * 100)),
        )
        new_right_pct = 100 - new_left_pct
        left = self.app.query_one(f"#{self._left_id}")
        right = self.app.query_one(f"#{self._right_id}")
        left.styles.width = f"{new_left_pct}%"
        right.styles.width = f"{new_right_pct}%"
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()
        self._dragging = False
        event.stop()


class HSplitter(Static):
    """Horizontal drag handle. Resizes the top/bottom widget pair above and below."""

    DEFAULT_CSS = """
    HSplitter {
        width: 100%;
        height: 1;
        background: $panel-darken-1;
        color: $panel-lighten-2;
    }
    HSplitter:hover {
        background: $accent;
    }
    """

    def __init__(self, top_id: str, bottom_id: str) -> None:
        super().__init__("─")
        self._top_id = top_id
        self._bottom_id = bottom_id
        self._dragging = False
        self._drag_start_y: int = 0
        self._top_start_h: int = 0
        self._bottom_start_h: int = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        top = self.app.query_one(f"#{self._top_id}")
        bottom = self.app.query_one(f"#{self._bottom_id}")
        self._dragging = True
        self._drag_start_y = event.screen_y
        self._top_start_h = top.size.height
        self._bottom_start_h = bottom.size.height
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = event.screen_y - self._drag_start_y
        top = self.app.query_one(f"#{self._top_id}")
        bottom = self.app.query_one(f"#{self._bottom_id}")
        total = self._top_start_h + self._bottom_start_h
        new_top = max(4, min(total - 4, self._top_start_h + delta))
        new_bottom = total - new_top
        top.styles.height = new_top
        bottom.styles.height = new_bottom
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()
        self._dragging = False
        event.stop()
