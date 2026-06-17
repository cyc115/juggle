"""Regression pin (2026-06-17): parse_summary_response failed on markdown-wrapped
headers — LLM emits **Context:**, ## Why:, - What:, etc. and the parser matched 0/4
sections, causing the modal to show the raw unreadable dump instead of the summary.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_topic_summary import parse_summary_response, summarize_topic


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_filled(sections: dict) -> bool:
    return all(sections.get(k, "").strip() for k in ("context", "why", "what", "result"))


# ---------------------------------------------------------------------------
# Baseline: bare headers still work (regression guard)
# ---------------------------------------------------------------------------

def test_parse_bare_headers():
    text = (
        "Context: some context here\n"
        "Why: why it was needed\n"
        "What: what changed\n"
        "Result: completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"bare headers should parse 4/4, got: {s}"


# ---------------------------------------------------------------------------
# Markdown variants — these FAIL before the fix
# ---------------------------------------------------------------------------

def test_parse_bold_headers():
    """**Context:** ... style — emitted by Claude when it adds markdown."""
    text = (
        "**Context:** some context here\n"
        "**Why:** why it was needed\n"
        "**What:** what changed\n"
        "**Result:** completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"**Header:** style should parse 4/4, got: {s}"


def test_parse_bold_no_colon_after():
    """**Context**: (colon outside the bold) style."""
    text = (
        "**Context**: some context here\n"
        "**Why**: why it was needed\n"
        "**What**: what changed\n"
        "**Result**: completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"**Header**: style should parse 4/4, got: {s}"


def test_parse_heading_headers():
    """## Context: style (markdown heading)."""
    text = (
        "## Context: some context here\n"
        "## Why: why it was needed\n"
        "## What: what changed\n"
        "## Result: completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"## Header: style should parse 4/4, got: {s}"


def test_parse_bullet_headers():
    """- Context: style (bullet list)."""
    text = (
        "- Context: some context here\n"
        "- Why: why it was needed\n"
        "- What: what changed\n"
        "- Result: completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"- Header: style should parse 4/4, got: {s}"


def test_parse_preamble_then_markdown():
    """LLM preamble line then markdown headers — the real-world failure case."""
    text = (
        "Here is the summary:\n"
        "\n"
        "**Context:** some context here\n"
        "**Why:** why it was needed\n"
        "**What:** what changed\n"
        "**Result:** completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"preamble + **Header:** style should parse 4/4, got: {s}"


def test_parse_case_insensitive():
    """CONTEXT: (uppercase) should also parse."""
    text = (
        "CONTEXT: some context here\n"
        "WHY: why it was needed\n"
        "WHAT: what changed\n"
        "RESULT: completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"uppercase headers should parse 4/4, got: {s}"


def test_parse_context_dash_style():
    """Context - text (dash instead of colon) style."""
    text = (
        "Context - some context here\n"
        "Why - why it was needed\n"
        "What - what changed\n"
        "Result - completed successfully"
    )
    s = parse_summary_response(text)
    assert _all_filled(s), f"Context - style should parse 4/4, got: {s}"


# ---------------------------------------------------------------------------
# End-to-end: summarize_topic with markdown-returning llm_fn
# ---------------------------------------------------------------------------

def test_summarize_topic_with_markdown_llm():
    """summarize_topic must return non-empty sections when LLM emits markdown headers."""
    markdown_response = (
        "Here is the summary:\n\n"
        "**Context:** The project needed a config refactor.\n"
        "**Why:** Old config was hard to extend.\n"
        "**What:** Extracted settings to juggle_settings.py.\n"
        "**Result:** All tests pass, config is cleaner."
    )

    sections = summarize_topic(
        task_input="refactor config",
        result_output="done",
        messages=[],
        meta={"label": "T1", "title": "Config refactor", "status": "verified"},
        llm_fn=lambda prompt: markdown_response,
    )
    assert _all_filled(sections), (
        f"summarize_topic must return 4/4 filled sections for markdown LLM output, got: {sections}"
    )


# ---------------------------------------------------------------------------
# _apply_summary branch: any_content check passes for markdown input
# ---------------------------------------------------------------------------

def test_apply_summary_takes_summary_branch_for_markdown():
    """After fix, _apply_summary must take the summary branch (not raw fallback)
    when summarize_topic returns non-empty sections from markdown LLM output."""
    markdown_response = (
        "**Context:** ctx\n**Why:** why\n**What:** what\n**Result:** done"
    )
    sections = summarize_topic(
        task_input="x",
        result_output="y",
        messages=[],
        meta={"label": "T1", "title": "t", "status": "done"},
        llm_fn=lambda p: markdown_response,
    )
    any_content = any(
        (sections.get(k) or "").strip()
        for k in ("context", "why", "what", "result")
    )
    assert any_content, (
        "_apply_summary any_content check should be True when sections are filled"
    )
