"""Juggle Cockpit Format — pure display/priority helpers.

Owns the age-string formatter (``format_age``) and the thread display-priority
tier computation (``priority_tier`` + its TIER_* constants). Extracted from
juggle_cockpit_model (P5a mechanical, 2026-07-05) so the snapshot module keeps a
single concern (DB reads → typed dataclasses) and stays under its LOC budget.
Zero DB, zero Rich imports. Re-exported by juggle_cockpit_model for back-compat.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# format_age
# ---------------------------------------------------------------------------


def format_age(secs: int | None) -> str:
    """Convert seconds to compact age string: '12s', '5m', '2h', '3d'."""
    if secs is None:
        return "—"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# ---------------------------------------------------------------------------
# priority_tier
# ---------------------------------------------------------------------------

TIER_BLOCKER = 0
TIER_REVIEW = 1
TIER_BACKGROUND = 2
TIER_CURRENT = 3
TIER_IDLE = 5
TIER_DONE = 6

_IDLE_THRESHOLD_SECS = 2 * 3600  # 2 hours


def priority_tier(
    agent_result: str | None,
    status: str,
    last_active_age_secs: int | None,
    is_current: bool,
    reviewed: bool = False,
) -> int:
    """Compute display-priority tier for a thread. Lower = higher priority."""
    result = agent_result or ""

    if result.startswith("⚠️ BLOCKER:"):
        return TIER_BLOCKER

    if status == "done" and result and not is_current and not reviewed:
        return TIER_REVIEW

    if status == "background":
        return TIER_BACKGROUND

    if is_current:
        return TIER_CURRENT

    if last_active_age_secs is not None and last_active_age_secs > _IDLE_THRESHOLD_SECS:
        return TIER_IDLE

    if status == "done":
        return TIER_DONE

    return TIER_IDLE
