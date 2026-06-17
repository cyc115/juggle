"""TDD regression-pin tests for thread dedup scorer (token-set Jaccard + guards).

Backtest over 730 historical topics drove the exact fixtures below.
RED before scorer changes; GREEN after.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

THRESHOLD = 2 / 3  # reuse threshold — same value as THREAD_DEDUP_THRESHOLD


# ---------------------------------------------------------------------------
# Cycle 1 — MUST-MERGE pairs (score >= threshold → dedup fires)
# ---------------------------------------------------------------------------

MUST_MERGE = [
    ("tmpfs in-memory DB mode", "implement tmpfs DB mode"),
    ("tmux agent teams — design + impl plan", "implement tmux agent teams"),
    ("talkback spec", "implement talkback"),
    ("Split juggle_cli.py into modules", "split-juggle-cli refactor"),
    (
        "Bootstrap optimization research + design doc",
        "Bootstrap optimization B and C implementation",
    ),
    ("Fix watchdog persistence", "Watchdog supervisor persistence"),
    (
        "Prefix orchestrator responses with topic ID",
        "Orchestrator responses prefixed with topic ID",
    ),
]


@pytest.mark.parametrize("a,b", MUST_MERGE)
def test_must_merge_pairs_score_at_or_above_threshold(a, b):
    """Spec/impl pairs of the SAME feature must score >= THRESHOLD so dedup fires.

    2026-06-17: old containment scorer (threshold 0.80) missed these because
    action verbs diluted the lexical ratio.
    """
    from dbops.threads import _title_similarity

    score = _title_similarity(a, b)
    assert score >= THRESHOLD, (
        f"Expected score >= {THRESHOLD}, got {score:.3f} for {a!r} vs {b!r}"
    )


# ---------------------------------------------------------------------------
# Cycle 2 — MUST-NOT-MERGE pairs (score < threshold → distinct topics kept)
# ---------------------------------------------------------------------------

MUST_NOT_MERGE = [
    ("idea loop 1", "idea loop 2"),
    ("idea loop 4", "idea loop shakeout"),
    ("textual migration phase 1", "textual migration phase 2"),
    ("ui retrofit s1", "ui retrofit s2"),
    ("AWS", "LifeOS AWS cost reduction"),
    ("CI", "pr462-ci-fix"),
    ("bench-symbol-grep", "bench-symbol-semble"),
    ("Trading Edge Time Column", "Trading Edge Direction Color Column"),
]


@pytest.mark.parametrize("a,b", MUST_NOT_MERGE)
def test_must_not_merge_pairs_score_below_threshold(a, b):
    """Distinct-iteration and short-stub pairs must score < THRESHOLD.

    2026-06-17: old containment scorer false-merged short stubs (AWS, CI)
    and identical-except-for-series titles (loop 1/2, phase 1/2).
    """
    from dbops.threads import _title_similarity

    score = _title_similarity(a, b)
    assert score < THRESHOLD, (
        f"Expected score < {THRESHOLD}, got {score:.3f} for {a!r} vs {b!r}"
    )


# ---------------------------------------------------------------------------
# Cycle 3 — numbered-series guard returns 0 for sequence variants
# ---------------------------------------------------------------------------

SERIES_PAIRS = [
    ("idea loop 1", "idea loop 2"),
    ("idea loop 4", "idea loop shakeout"),
    ("textual migration phase 1", "textual migration phase 2"),
    ("ui retrofit s1", "ui retrofit s2"),
    ("cockpit flicker", "cockpit flicker v2"),
]


@pytest.mark.parametrize("a,b", SERIES_PAIRS)
def test_numbered_series_guard_returns_zero(a, b):
    """Numbered-series guard must return exactly 0.0 for sequence variants.

    2026-06-17: sequence iterations should never be merged regardless of
    token overlap — they represent distinct work phases.
    """
    from dbops.threads import _title_similarity

    assert _title_similarity(a, b) == 0.0, (
        f"Expected 0.0 (series guard) for {a!r} vs {b!r}"
    )


# ---------------------------------------------------------------------------
# Cycle 4 — threshold constant is 0.667
# ---------------------------------------------------------------------------


def test_dedup_threshold_is_two_thirds():
    """THREAD_DEDUP_THRESHOLD must be 2/3 (backtest sweet-spot, exact fraction)."""
    from dbops.threads import THREAD_DEDUP_THRESHOLD

    assert abs(THREAD_DEDUP_THRESHOLD - 2 / 3) < 1e-9, (
        f"Expected 2/3 (~0.6667), got {THREAD_DEDUP_THRESHOLD}"
    )


# ---------------------------------------------------------------------------
# Cycle 5 — title-gen does NOT disambiguate near-duplicate titles
# ---------------------------------------------------------------------------


def test_generate_title_skips_disambiguation_for_near_duplicate(tmp_path):
    """Title generator must not push near-duplicate titles apart.

    2026-06-17: _dedupe_title was called unconditionally, actively fighting
    dedup by making spec/impl titles different enough to avoid merging.
    """
    from unittest.mock import MagicMock, patch

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from juggle_cli_common import _generate_title_for_thread

    db = MagicMock()
    db.get_all_threads.return_value = [
        {"id": "other-uuid", "title": "Implement Talkback Feature"},
    ]
    db.update_thread = MagicMock()

    # LLM returns a title that is near-duplicate of the existing one
    with patch("juggle_cli_common._cheap_llm_call", return_value="Talkback Feature Implementation"):
        result = _generate_title_for_thread(db, "my-uuid", "implement talkback feature")

    # Must NOT have "(Something)" appended by disambiguation
    assert "(" not in result, (
        f"Title was disambiguated when it should not have been: {result!r}"
    )
