"""juggle_cockpit_layout — Column-ratio helpers for the cockpit splitter panels.

Owns: constants for column width floors, _sanitize_col_ratios, _clamp_col_pct,
_compute_ratios, _write_ratios.  These are pure functions (no DB, no Textual).
Must not own: CockpitApp, rendering, snapshots, profiling.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Column-width floor constants
# ---------------------------------------------------------------------------

_MIN_TOPICS_RATIO: float = 0.15   # topics pane minimum fraction of total width
_MIN_ACTIONS_RATIO: float = 0.15  # actions pane minimum fraction of right width
_MIN_AGENTS_RATIO: float = 0.10   # agents pane minimum fraction of right width
_MIN_TOPICS_PCT: int = 15         # apply-site minimum for topics as integer percent
_MAX_TOPICS_PCT: int = 60         # apply-site maximum for topics as integer percent
_DEFAULT_COL_RATIOS: list[float] = [0.30, 0.40, 0.30]


def _sanitize_col_ratios(ratios: object) -> list[float]:
    """Validate and floor column_ratios loaded from config.

    Returns ratios unchanged if healthy; falls back to _DEFAULT_COL_RATIOS
    when the list is wrong length, any column is below its floor, or the
    sum is far from 1.0.  Self-heals existing corrupted configs on load.
    """
    try:
        lst = list(ratios)
    except TypeError:
        return list(_DEFAULT_COL_RATIOS)
    if len(lst) != 3:
        return list(_DEFAULT_COL_RATIOS)
    t, a, ag = (float(x) for x in lst)
    if not (0.9 <= t + a + ag <= 1.1):
        return list(_DEFAULT_COL_RATIOS)
    if t < _MIN_TOPICS_RATIO or a < _MIN_ACTIONS_RATIO or ag < _MIN_AGENTS_RATIO:
        return list(_DEFAULT_COL_RATIOS)
    return [t, a, ag]


def _clamp_col_pct(pct: int, lo: int = _MIN_TOPICS_PCT, hi: int = _MAX_TOPICS_PCT) -> int:
    """Clamp an integer percent to [lo, hi].  Prevents 0% or 100% topics."""
    return max(lo, min(hi, pct))


def _compute_ratios(topics_cells: float, actions_cells: float, agents_cells: float) -> list[float]:
    """Normalize actual rendered cell widths to [topics, actions, agents] ratios summing to 1.0.

    Uses size.width (absolute cells) so the result is correct regardless of whether
    styles were set as percent (initial mount) or as cell integers (post-drag).
    Guarantees each column >= its floor: floors are satisfied first, then the
    remaining space is distributed proportionally among all columns.  Prevents
    any column from being persisted as 0 even when physically collapsed.
    """
    total = topics_cells + actions_cells + agents_cells
    if total <= 0:
        return []
    t = topics_cells / total
    a = actions_cells / total
    ag = 1.0 - t - a
    # Distribute space: each column gets its floor guarantee first, then the
    # leftover is shared proportionally to how much each column *exceeds* its floor.
    floors = (_MIN_TOPICS_RATIO, _MIN_ACTIONS_RATIO, _MIN_AGENTS_RATIO)
    raw = (t, a, ag)
    above = tuple(max(0.0, r - f) for r, f in zip(raw, floors))
    above_total = sum(above)
    remaining = 1.0 - sum(floors)  # space available beyond the guaranteed floors
    if above_total > 0:
        result = [f + ab / above_total * remaining for f, ab in zip(floors, above)]
    else:
        # All columns collapsed to zero or below floors — normalize floors directly
        fs = sum(floors)
        result = [f / fs for f in floors]
    t, a, ag = result
    t = round(t, 2)
    a = round(a, 2)
    ag = round(1.0 - t - a, 2)
    return [t, a, ag]


def _write_ratios(config_path: Path, ratios: list[float]) -> None:
    """Atomically write column_ratios to config.json.

    No-op if config file is missing or the cockpit key is absent — avoids
    corrupting a partially-edited config on first run. Atomic via tmp + os.replace.
    """
    if not config_path.exists():
        return
    try:
        cfg = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if "cockpit" not in cfg:
        return
    cfg["cockpit"]["column_ratios"] = ratios
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, config_path)
