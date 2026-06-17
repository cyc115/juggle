"""TDD tests for juggle_topic_summary — LLM-driven topic info modal summarizer.

Tests run without network: LLM calls are injected/mocked.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MESSAGES = [
    {"role": "user", "content": "Implement a caching layer for the DB queries."},
    {"role": "assistant", "content": "I'll start by writing the failing test."},
    {"role": "assistant", "content": "Done. Added LRU cache in db_cache.py. Tests pass."},
]

SAMPLE_META = {
    "label": "AX",
    "title": "db caching layer",
    "status": "closed",
}

SAMPLE_TASK_INPUT = "Implement a caching layer for the DB queries."
SAMPLE_RESULT_OUTPUT = "Done. Added LRU cache in db_cache.py. Tests pass."

SAMPLE_LLM_RESPONSE = """\
Context: This topic is part of the juggle project's performance initiative to speed up repeated DB reads.
Why: Repeated queries to the DB were slow; a cache would avoid redundant round-trips.
What: An LRU cache was added in db_cache.py wrapping the main query functions.
Result: Completed successfully. Tests pass. No follow-up needed."""


# ---------------------------------------------------------------------------
# Cycle 1 — build_summarize_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_contains_key_fields():
    """build_summarize_prompt includes label, task input, and result in output."""
    from juggle_topic_summary import build_summarize_prompt

    prompt = build_summarize_prompt(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META)

    assert "AX" in prompt
    assert "caching layer" in prompt
    assert "LRU cache" in prompt


def test_build_prompt_is_pure():
    """build_summarize_prompt returns same output for same inputs (no side effects)."""
    from juggle_topic_summary import build_summarize_prompt

    p1 = build_summarize_prompt(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META)
    p2 = build_summarize_prompt(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META)
    assert p1 == p2


def test_build_prompt_empty_inputs():
    """build_summarize_prompt handles empty task_input and result_output gracefully."""
    from juggle_topic_summary import build_summarize_prompt

    prompt = build_summarize_prompt("", "", [], {"label": "Z", "title": "", "status": "open"})
    assert isinstance(prompt, str)
    assert len(prompt) > 0


# ---------------------------------------------------------------------------
# Cycle 2 — parse_summary_response
# ---------------------------------------------------------------------------


def test_parse_summary_all_four_sections():
    """parse_summary_response extracts all four sections from well-formed LLM output."""
    from juggle_topic_summary import parse_summary_response

    sections = parse_summary_response(SAMPLE_LLM_RESPONSE)

    assert sections["context"] != ""
    assert sections["why"] != ""
    assert sections["what"] != ""
    assert sections["result"] != ""


def test_parse_summary_correct_content():
    """parse_summary_response maps content to the right section keys."""
    from juggle_topic_summary import parse_summary_response

    sections = parse_summary_response(SAMPLE_LLM_RESPONSE)

    assert "juggle" in sections["context"] or "performance" in sections["context"]
    assert "slow" in sections["why"] or "cache" in sections["why"]
    assert "LRU" in sections["what"]
    assert "Completed" in sections["result"] or "pass" in sections["result"]


def test_parse_summary_missing_section_returns_empty():
    """parse_summary_response returns '' for missing sections (partial LLM output)."""
    from juggle_topic_summary import parse_summary_response

    partial = "Context: Some context.\nWhy: Some reason."
    sections = parse_summary_response(partial)

    assert sections["context"] != ""
    assert sections["why"] != ""
    assert sections["what"] == ""
    assert sections["result"] == ""


def test_parse_summary_empty_response():
    """parse_summary_response returns all-empty dict for empty LLM text."""
    from juggle_topic_summary import parse_summary_response

    sections = parse_summary_response("")
    assert all(v == "" for v in sections.values())
    assert set(sections.keys()) == {"context", "why", "what", "result"}


# ---------------------------------------------------------------------------
# Cycle 3 — summarize_topic (injectable llm_fn)
# ---------------------------------------------------------------------------


def test_summarize_topic_uses_llm_fn():
    """summarize_topic calls the injected llm_fn and returns parsed sections."""
    from juggle_topic_summary import summarize_topic

    calls = []

    def mock_llm(prompt: str) -> str:
        calls.append(prompt)
        return SAMPLE_LLM_RESPONSE

    sections = summarize_topic(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META, llm_fn=mock_llm)

    assert len(calls) == 1
    assert sections["context"] != ""
    assert sections["why"] != ""
    assert sections["what"] != ""
    assert sections["result"] != ""


def test_summarize_topic_fallback_on_llm_error():
    """summarize_topic returns empty-string dict when llm_fn raises."""
    from juggle_topic_summary import summarize_topic

    def bad_llm(prompt: str) -> str:
        raise RuntimeError("network unavailable")

    sections = summarize_topic(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META, llm_fn=bad_llm)

    assert isinstance(sections, dict)
    assert set(sections.keys()) == {"context", "why", "what", "result"}
    # All empty on error — modal will show raw fallback
    assert all(v == "" for v in sections.values())


def test_summarize_topic_fallback_on_llm_returns_none():
    """summarize_topic handles llm_fn returning None gracefully."""
    from juggle_topic_summary import summarize_topic

    sections = summarize_topic(SAMPLE_TASK_INPUT, SAMPLE_RESULT_OUTPUT, SAMPLE_MESSAGES, SAMPLE_META, llm_fn=lambda p: None)

    assert isinstance(sections, dict)
    assert set(sections.keys()) == {"context", "why", "what", "result"}


# ---------------------------------------------------------------------------
# Cycle 4 — format_recent_activity
# ---------------------------------------------------------------------------


def test_format_recent_activity_basic():
    """format_recent_activity returns one bullet per message, trimmed."""
    from juggle_topic_summary import format_recent_activity

    lines = format_recent_activity(SAMPLE_MESSAGES)

    assert len(lines) == len(SAMPLE_MESSAGES)
    assert all(isinstance(l, str) for l in lines)
    assert "[user]" in lines[0]
    assert "[assistant]" in lines[1]


def test_format_recent_activity_limit():
    """format_recent_activity returns at most `limit` items (last N)."""
    from juggle_topic_summary import format_recent_activity

    msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    lines = format_recent_activity(msgs, limit=3)

    assert len(lines) == 3
    assert "msg 7" in lines[0]
    assert "msg 9" in lines[2]


def test_format_recent_activity_truncates_long_content():
    """format_recent_activity truncates long message content with ellipsis."""
    from juggle_topic_summary import format_recent_activity

    long_msg = [{"role": "user", "content": "x" * 300}]
    lines = format_recent_activity(long_msg)

    assert len(lines[0]) < 200
    assert lines[0].endswith("…")


def test_format_recent_activity_empty():
    """format_recent_activity returns [] for empty message list."""
    from juggle_topic_summary import format_recent_activity

    assert format_recent_activity([]) == []
