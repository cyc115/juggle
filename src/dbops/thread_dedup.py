"""dbops.thread_dedup — pure lexical thread-title dedup scorer (v1, NO LLM).

Extracted from dbops.threads (loc-gate budget, P8 Wave-3) so the create path's
duplicate-detection stays a self-contained, swappable unit. A new thread whose
title is a strong lexical match of an OPEN same-project thread is treated as a
semantic duplicate; create_thread reuses the existing thread instead of spawning
a twin. Kept PURE so a future semantic/embedding scorer can replace it without
touching the call sites.
"""
from __future__ import annotations

import re

# Reuse threshold on the 0..1 similarity score. >= this is a duplicate.
# 2/3 is the backtest sweet-spot (~84% precision). Several key pairs score
# exactly 2/3, so the threshold must be <= 2/3 (not the truncated 0.667).
THREAD_DEDUP_THRESHOLD = 2 / 3

# Leading "[T-<id>] " graph-topic prefix stamped onto dispatch-thread titles.
_TOPIC_PREFIX_RE = re.compile(r"^\s*\[t-[^\]]+\]\s*", re.IGNORECASE)

# Action verbs and phase labels that describe HOW work is done, not WHAT it is.
# Dropping them lets spec↔impl titles match on shared content words.
_DEDUP_ACTION_VERBS = frozenset({
    "implement", "fix", "add", "spec", "build", "update", "rebind", "make",
    "create", "remove", "improve", "refactor", "untruncate", "wire", "enable",
    "support", "handle", "setup", "set", "configure", "integrate", "migrate",
    "tweak", "change", "new", "topic", "design", "impl", "finish", "verify",
    "investigate", "debug", "audit", "review", "clean", "cleanup", "extract",
    "split", "move", "replace", "automate", "tighten", "bump",
    # Extended (sensible additions):
    "implementation", "research", "prefix",
})

# Function words and structural filler that carry no topical signal.
_DEDUP_STOPWORDS = frozenset({
    "the", "a", "an", "to", "for", "of", "in", "on", "and", "or", "with",
    "mode", "modal", "via", "is", "into", "from", "be", "our", "my", "its",
    "plan", "doc", "docs",
    # Structural result-type nouns (describe the artefact kind, not the topic):
    "modules",
})

# Trailing sequence markers: trailing integer, phase/part/vN/sN/iteration N,
# or ordinal words used as series suffix. Titles differing ONLY by these are
# distinct iterations and must never be merged.
_SEQ_MARKER_RE = re.compile(
    r"(?:"
    r"\s+(?:phase|part|iteration|step|version)\s*\d+"  # "phase 2", "step 3"
    r"|\s+v\d+"                                         # "v2", "v10"
    r"|\s+s\d+"                                         # "s1", "s3"
    r"|\s+\d+"                                          # bare trailing integer
    r"|\s+(?:shakeout|two|three|four|five|six|seven|eight|nine|ten)"
    r")$",
    re.IGNORECASE,
)


def _normalize_title_str(title: str) -> str:
    """Return a normalized bare string for sequence-marker comparison."""
    s = (title or "").lower()
    s = _TOPIC_PREFIX_RE.sub("", s)
    s = re.sub(r"[-_/]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return s.strip()


def _strip_sequence_marker(s: str) -> tuple[str, str]:
    """Strip trailing sequence marker. Returns (base, marker_or_empty)."""
    m = _SEQ_MARKER_RE.search(s)
    if m:
        return s[: m.start()].rstrip(), m.group(0).strip().lower()
    return s, ""


def _normalize_title_tokens(title: str) -> set[str]:
    """Tokenize to significant content words: drop verbs, stopwords, len-1 tokens."""
    s = _normalize_title_str(title)
    drop = _DEDUP_ACTION_VERBS | _DEDUP_STOPWORDS
    return {tok for tok in s.split() if len(tok) > 1 and tok not in drop}


def _title_similarity(a: str, b: str) -> float:
    """Token-set Jaccard similarity with numbered-series guard.

    Normalise → drop action verbs, stopwords, and single-char tokens →
    Jaccard = |A∩B|/|A∪B|.

    Numbered-series guard: if both titles are identical once a trailing
    sequence marker (integer, phase N, sN, vN, …) is stripped AND the
    markers differ, they are distinct iterations → score 0 (never merge).
    """
    na = _normalize_title_str(a)
    nb = _normalize_title_str(b)

    # Numbered-series guard — check before tokenisation
    a_base, a_marker = _strip_sequence_marker(na)
    b_base, b_marker = _strip_sequence_marker(nb)
    if a_base == b_base and a_base and a_marker != b_marker:
        return 0.0

    ta = _normalize_title_tokens(a)
    tb = _normalize_title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0

    # Subset bonus: when one token set is entirely contained in the other
    # AND the smaller set has >= 2 tokens, score 1.0.  This catches the
    # terse-label ↔ long-dispatch-title pattern ("slug wheel" ↔ full
    # dispatch title) without the single-token false-merges ("AWS" ↔
    # "LifeOS AWS cost reduction") that plagued the old containment scorer.
    min_size = min(len(ta), len(tb))
    if inter == min_size >= 2:
        return 1.0

    return inter / len(ta | tb)  # Jaccard
