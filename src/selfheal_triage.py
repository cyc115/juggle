"""selfheal_triage — pure, deterministic triage logic for self-heal v2 P1.

No DB, no I/O. Owns the anchored deterministic allowlist, the strong-real-bug
signal detector, diagnoser signal-strength ordering, and the re-surface valve
predicates. Kept separate from juggle_selfheal.py to honor the ≤300-line gate.
"""
from __future__ import annotations

import re
from collections import namedtuple
from datetime import datetime, timezone

# Bump whenever ALLOWLIST_RULES changes — recorded with every sweep audit line
# and (future) used by the lease so older-version classifications re-confirm.
ALLOWLIST_VERSION = 1

AllowlistRule = namedtuple("AllowlistRule", "rule_id exc_type entrypoint text_regex")

# Strong real-bug signal: a malformed juggle_cli.py self-call. NEVER swept;
# the diagnoser prioritizes these. Anchored on argparse's stable error phrasings.
STRONG_SIGNAL_REGEX = re.compile(
    r"invalid choice|the following arguments are required|unrecognized arguments",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Lowercase, strip digits, collapse whitespace — mirrors capture normalization."""
    t = re.sub(r"\d+", "", (text or "")[:500].lower())
    return re.sub(r"\s+", " ", t).strip()


# Anchored allowlist. exc_type/entrypoint None == wildcard for that field; the
# regex runs on normalized text. Seed set from spec §4.3.
#
# NOTE (deviation from plan seed, reconciled with the §7 pins): B-class DB rows
# carry exc_type=None (error_class 'B' is NOT stored in exc_type), so anchoring a
# rule on exc_type='B' would never match a real swept row. The transient
# tool-exit rules are therefore anchored on the ENTRYPOINT ('Bash') instead —
# this also makes test_classify_rejects_unanchored_substring meaningful: a
# 'broken pipe' string arriving from a DIFFERENT entrypoint must NOT be swept.
ALLOWLIST_RULES: tuple[AllowlistRule, ...] = (
    AllowlistRule("tmp_path_gone", None, None,
                  re.compile(r"no such file or directory: /tmp/juggle-")),
    AllowlistRule("sleep_timeout", None, "Bash",
                  re.compile(r"sleep: command timed out|sleep.*timed out")),
    AllowlistRule("broken_pipe", None, "Bash",
                  re.compile(r"broken pipe|\benobufs\b")),
    AllowlistRule("taskstop_dead", None, None,
                  re.compile(r"taskstop.*no such (task|process)|task already (dead|stopped)")),
)


def classify_allowlist(exc_type: str | None, entrypoint: str | None, text: str) -> str | None:
    """Return the matching allowlist rule_id, or None.

    Guard: a strong real-bug signal short-circuits to None (never swept) even if
    a benign regex would otherwise match (spec §3 verdict != origin).
    """
    raw = text or ""
    if STRONG_SIGNAL_REGEX.search(raw):
        return None
    norm = _norm(raw)
    for rule in ALLOWLIST_RULES:
        if rule.exc_type is not None and rule.exc_type != (exc_type or ""):
            continue
        if rule.entrypoint is not None and rule.entrypoint != (entrypoint or ""):
            continue
        if rule.text_regex.search(norm):
            return rule.rule_id
    return None


def signal_strength(row: dict) -> int:
    """Real-bug signal score for diagnoser ordering. Higher = investigate sooner."""
    text = (row.get("traceback") or "") + " " + (row.get("command_args") or "")
    if STRONG_SIGNAL_REGEX.search(text):
        return 3  # malformed juggle_cli self-call — strongest real-bug signal
    if (row.get("error_class") or "") == "A":
        return 2  # application exception
    return 1      # other B-class tool exit


def order_candidates(rows: list[dict]) -> list[dict]:
    """Stable order by (signal_strength DESC, count DESC) — anti-starvation (spec §4.3)."""
    return sorted(rows, key=lambda r: (signal_strength(r), r.get("count") or 0), reverse=True)
