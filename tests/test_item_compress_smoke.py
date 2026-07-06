"""ONE live-key smoke for the concise cockpit item formatter.

Skipped unless a real LLM key is present (the unit suite mocks the LLM). Proves
the few-shot compressor + constraint enforcer produce a budget-valid item from
the real canonical verbose input end-to-end.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_item_compress as jic
from test_item_compress import (
    VERBOSE_ACTION_INPUT,
    VERBOSE_NOTIFICATION_INPUT,
    _assert_action_ok,
    _assert_notification_ok,
)

_HAS_KEY = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENROUTER_KEY"))


@pytest.mark.skipif(not _HAS_KEY, reason="no LLM key — live compress smoke skipped")
def test_live_action_compress_smoke():
    out = jic.compress_item("action", "SL", VERBOSE_ACTION_INPUT, thread_ref="SL")
    _assert_action_ok(out)


@pytest.mark.skipif(not _HAS_KEY, reason="no LLM key — live compress smoke skipped")
def test_live_notification_compress_smoke():
    out = jic.compress_item(
        "notification", "SL", VERBOSE_NOTIFICATION_INPUT, thread_ref="SL"
    )
    _assert_notification_ok(out)
