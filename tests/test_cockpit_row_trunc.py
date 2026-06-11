"""Narrow Topics rows: RIGHT-truncate the title only; never drop age/emoji/[label]."""
import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from juggle_cockpit_model import Topic
from juggle_cockpit_view import render_topics

LONG = "Live TUI Control Server Screenshot and other very long words here"
GLYPH = "🏃"  # running glyph


def _render(width):
    topics = [Topic(id="t1", label="WB", status="running", age_secs=259200,
                    is_current=False, title=LONG)]
    c = Console(record=True, width=width)
    c.print(render_topics(topics, "wide"))  # terminal-wide bp; pane is narrow
    return c.export_text()


def test_emoji_never_truncated():
    # At widths that force truncation, the status glyph must survive — it sits
    # left of the title and right-crop removes from the right.
    for w in (44, 40, 36, 32):
        out = _render(w)
        assert GLYPH in out, f"emoji dropped at width {w}:\n{out}"


def test_label_never_truncated():
    for w in (44, 40, 36, 32):
        out = _render(w)
        assert "[WB]" in out, f"[WB] dropped at width {w}:\n{out}"


def test_no_leading_ellipsis_before_label():
    # the old multi-column bug produced "…[…" — left-side truncation. Forbidden.
    for w in (44, 40, 36, 32):
        out = _render(w)
        assert "…[" not in out, f"left-truncation at width {w}:\n{out}"


def test_title_right_truncates_with_ellipsis():
    out = _render(36)
    assert "…" in out, f"title should right-truncate with ellipsis:\n{out}"
    # the ellipsis must come AFTER the label (right side), never before it.
    line = next(l for l in out.splitlines() if "[WB]" in l)
    assert line.index("[WB]") < line.index("…"), f"ellipsis before label: {line!r}"
