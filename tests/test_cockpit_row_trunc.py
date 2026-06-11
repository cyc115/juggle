"""Narrow Topics rows WRAP (not truncate): glyph + [label] pinned, title folds."""
import sys
from pathlib import Path

from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from juggle_cockpit_model import Topic
from juggle_cockpit_view import render_topics

LONG = "Show readable topic label on in-progress graph nodes everywhere"


def _render_narrow(width=30):
    topics = [Topic(id="t1", label="WR", status="running", age_secs=120,
                    is_current=False, title=LONG)]
    c = Console(record=True, width=width)
    c.print(render_topics(topics, "narrow"))
    return c.export_text()


def test_narrow_keeps_label_visible():
    out = _render_narrow()
    assert "[WR]" in out, out


def test_narrow_no_leading_ellipsis_before_label():
    out = _render_narrow()
    # the old bug rendered "…[…" — a truncation ellipsis before the label
    assert "…[" not in out, out


def test_narrow_title_wraps_not_truncated():
    out = _render_narrow()
    # every word of the long title must survive (wrapped across lines), not
    # be cut off by an ellipsis.
    for word in LONG.split():
        assert word in out, f"missing '{word}' — title was truncated:\n{out}"


def test_narrow_title_continuation_is_indented():
    out = _render_narrow(width=28)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # find the row line containing [WR]; its wrapped continuation line should
    # be indented (leading whitespace) rather than starting at column 0.
    body = [ln for ln in lines if "│" in ln] or lines
    label_idx = next(i for i, ln in enumerate(body) if "[WR]" in ln)
    cont = body[label_idx + 1]
    inner = cont.split("│")[1] if "│" in cont else cont
    assert inner[:3].isspace(), f"continuation not indented: {cont!r}"
