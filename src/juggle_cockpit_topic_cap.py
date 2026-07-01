"""Topics-pane cap — pure truncation layer for the cockpit Topics pane.

Extracted from juggle_cockpit_model (LOC gate) so it is unit-testable without a
live cockpit. ``cap_topics`` truncates the ALREADY group+sorted topic list to at
most N, and ``resolve_max_topics`` reads N from env/config (default 30).

Policy ('2026-06-30 topics pane cap'):
  * NEVER drop a non-terminal topic (work in flight) — nor the current thread,
    nor a topic whose label is in ``protected_labels`` (a live/busy agent).
  * Once the total would exceed N, drop TERMINAL topics (done/closed/archived)
    OLDEST-FIRST (largest age_secs) until it fits. Silent — no '+N more' marker.
  * If non-terminal topics alone exceed N, the total stays over N (never drop
    them). N<=0 is treated as 'no cap'.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MAX_TOPICS = 30
_ENV_KEY = "JUGGLE_COCKPIT_MAX_TOPICS"

# Display statuses that must NEVER be dropped — the design's non-terminal node
# states (open/background/running/ready/dispatching/integrating) mapped through
# node_translation.status_for_state, plus the current-thread marker 'current'.
_NON_TERMINAL_STATUSES = frozenset(
    {
        "active",
        "background",
        "running",
        "current",
        "ready",
        "dispatching",
        "integrating",
    }
)


def cap_topics(ordered_topics, n, *, protected_labels=frozenset()):
    """Return ``ordered_topics`` truncated to at most ``n``, preserving order.

    Non-terminal / current / protected-label topics are kept unconditionally;
    terminal topics are removed oldest-first (largest ``age_secs``) only as needed
    to reach ``n``. ``n<=0`` (or ``None``) → identity.
    """
    topics = list(ordered_topics)
    if not n or n < 0 or len(topics) <= n:
        return topics

    def _droppable(t) -> bool:
        if getattr(t, "is_current", False):
            return False
        if t.label in protected_labels:
            return False
        return t.status not in _NON_TERMINAL_STATUSES

    droppable_idx = [i for i, t in enumerate(topics) if _droppable(t)]
    to_drop = min(len(topics) - n, len(droppable_idx))
    if to_drop <= 0:
        return topics

    # Oldest-first: largest age_secs first; stable tie-break on original index.
    droppable_idx.sort(key=lambda i: (-topics[i].age_secs, i))
    drop = set(droppable_idx[:to_drop])
    return [t for i, t in enumerate(topics) if i not in drop]


def _coerce_int(value):
    """Return int(value) or None if it is missing / not a clean integer."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def resolve_max_topics(env=None, config=None) -> int:
    """Resolve N: env ``JUGGLE_COCKPIT_MAX_TOPICS`` > config ``cockpit.max_topics``
    > ``DEFAULT_MAX_TOPICS``. Invalid values fall back (never crash).

    ``env`` / ``config`` are injectable for tests; when omitted they are read from
    ``os.environ`` and ``~/.juggle/config.json`` (``_JUGGLE_CONFIG_PATH`` override).
    """
    if env is None:
        env = os.environ
    if config is None:
        config = _load_config(env)

    from_env = _coerce_int(env.get(_ENV_KEY))
    if from_env is not None:
        return from_env

    cockpit = config.get("cockpit") if isinstance(config, dict) else None
    if isinstance(cockpit, dict):
        from_cfg = _coerce_int(cockpit.get("max_topics"))
        if from_cfg is not None:
            return from_cfg

    return DEFAULT_MAX_TOPICS


def _load_config(env) -> dict:
    """Read ~/.juggle/config.json (``_JUGGLE_CONFIG_PATH`` override). {} on error."""
    path = Path(
        env.get("_JUGGLE_CONFIG_PATH", str(Path.home() / ".juggle" / "config.json"))
    )
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}
