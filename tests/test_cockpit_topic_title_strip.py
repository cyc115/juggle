"""Tests for strip_leading_tag render helper and its application in render_topics."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import io
import pytest

pytest.importorskip("rich")

from rich.console import Console

from juggle_cockpit_view import strip_leading_tag, render_topics
from juggle_cockpit_model import Topic


# ---------------------------------------------------------------------------
# Unit tests for strip_leading_tag
# ---------------------------------------------------------------------------


def test_strip_node_id_prefix():
    assert strip_leading_tag("[T-fix-multirepo-project-root] do the thing") == "do the thing"


def test_strip_short_tag():
    assert strip_leading_tag("[task1] Armed-set accessors") == "Armed-set accessors"


def test_no_bracket_unchanged():
    assert strip_leading_tag("trading-edge spark NaN crash") == "trading-edge spark NaN crash"


def test_only_tag_returns_original():
    assert strip_leading_tag("[x]") == "[x]"


def test_only_one_leading_tag_stripped():
    assert strip_leading_tag("[a] [b] x") == "[b] x"


def test_empty_string():
    assert strip_leading_tag("") == ""


def test_none_like_empty():
    assert strip_leading_tag(None) == ""


# ---------------------------------------------------------------------------
# Render-level: [label] column unchanged; title no longer shows [T-...] tag
# ---------------------------------------------------------------------------


def _render_topics_plain(topics, bp="wide") -> str:
    panel = render_topics(topics, bp)
    buf = io.StringIO()
    console = Console(file=buf, width=120, no_color=True, highlight=False)
    console.print(panel)
    return buf.getvalue()


def test_render_label_column_present_and_title_stripped():
    t = Topic(
        id="t1",
        label="BP",
        title="[T-fix-multirepo-project-root] do the thing",
        status="running",
        age_secs=60,
        is_current=False,
    )
    rendered = _render_topics_plain([t])
    assert "[BP]" in rendered, "label column must still show [BP]"
    assert "[T-fix-multirepo-project-root]" not in rendered, "prefixed tag must be stripped from title"
    assert "do the thing" in rendered, "actual title text must appear"
