"""juggle_watchdog_paneparse — pure tmux pane-content parsing helpers.

Extracted from juggle_watchdog (loc-gate budget, 2026-07-08) — these six
functions + their regex/marker constants had no dependency on the rest of the
watchdog module. Re-exported from juggle_watchdog so the existing
juggle_watchdog.X import/patch surface (tests, juggle_watchdog_inspect.py)
keeps working unchanged.
"""
from __future__ import annotations

import hashlib as _hashlib
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BOX_TOP_RE = re.compile(r"^╭─+╮\s*$")
_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")

# Matches the CC pane footer context usage: e.g. "Sonnet 4.6(164.0k/200.0k)"
_CTX_USAGE_RE = re.compile(r"\((\d+(?:\.\d+)?)(k?)/(\d+(?:\.\d+)?)(k?)\)")

# Matches CC thinking spinner: timer pattern "(26s ·" / "(6m 17s ·" or known
# thinking-word synonyms. Timer detection is generic; synonyms are a fallback.
_THINKING_RE = re.compile(
    r"(?:"
    r"\(\d+(?:m \d+)?s[\s\xb7]"  # (26s · or (6m 17s · (U+00B7 middle dot)
    r"|\bThinking\b"
    r"|\b(?:Befuddling|Burrowing|Saut[eé]ed|Cooked|Churned|Brewed|Baked|Crunched?"
    r"|Garnishing|Newspapering|Stewing|Billowing|Sprouting|Warping)\b"
    r")"
)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _hash_tail(content: str, lines: int = 10) -> str:
    tail = "\n".join(content.splitlines()[-lines:])
    return _hashlib.sha256(tail.encode()).hexdigest()[:16]


def _has_execution_markers(tail: str) -> bool:
    return any(m in tail for m in _EXECUTION_MARKERS)


def _parse_context_pct(content: str) -> float | None:
    """Parse context usage fraction from a CC pane footer.

    Matches patterns like 'Sonnet 4.6(164.0k/200.0k)'.
    Returns float in [0, 1], or None if not parseable.
    """
    m = _CTX_USAGE_RE.search(content)
    if not m:
        return None
    used_val, used_k, total_val, total_k = m.groups()
    used = float(used_val) * (1000.0 if used_k else 1.0)
    total = float(total_val) * (1000.0 if total_k else 1.0)
    if total == 0:
        return None
    return used / total


def _has_active_spinner(content: str) -> bool:
    """Return True if content shows a CC active-thinking spinner or timer."""
    return bool(_THINKING_RE.search(content))


def _has_box_top(content: str) -> bool:
    return any(_BOX_TOP_RE.match(line) for line in content.splitlines())
