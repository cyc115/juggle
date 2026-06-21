"""selfheal_grouping — pure deterministic error grouping (normalize → hash).

No DB, no I/O. Rollbar-style: mask volatile tokens, hash the stable remainder,
with error_class/exc_type/entrypoint as HARD never-normalized partitions.

Two-level model (research §5.5, LOAD-BEARING): the exact ``signature_hash`` stays
the immutable occurrence identity; ``group_key`` is a DERIVED, recomputable view
that collapses line-number drift ("8 hashes = 1 bug") WITHOUT over-aggregating.
Over-aggregation is the asymmetric danger (it silently swallows new bugs), so the
normalizer is deliberately CONSERVATIVE: it biases to under-normalize.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# Order is load-bearing: specific patterns BEFORE the generic NUM rule.
_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<UUID>'),
    (re.compile(r'\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b'), '<TS>'),
    (re.compile(r'\b0x[0-9a-fA-F]+\b'), '<HEX>'),
    (re.compile(r'%\d+\b'), '<PANE>'),                      # tmux pane %NNNN
    (re.compile(r'(?:/[^/\s:]+)+/?'), '<PATH>'),            # unix paths (incl /tmp/juggle-*)
    (re.compile(r'\b[0-9a-fA-F]{12,40}\b'), '<HASH>'),      # git sha / long hex
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), '<IP>'),
    (re.compile(r'\bpid[ =:]+\d+\b', re.I), 'pid=<PID>'),
    (re.compile(r'\b\d{4,}\b'), '<NUM>'),                   # 4+ digit runs ONLY → keep <=3-digit codes
]


def normalize(text: str) -> str:
    """Mask volatile tokens; preserve <=3-digit codes (HTTP/exit) as discriminators."""
    t = text or ""
    for pat, repl in _RULES:
        t = pat.sub(repl, t)
    return re.sub(r'\s+', ' ', t).strip()


def normalize_entrypoint(ep: str | None) -> str:
    """``normalize(basename(ep))`` lowercased; ``""`` for None (a HARD partition)."""
    if not ep:
        return ""
    base = Path(ep).name if "/" in ep else ep
    return normalize(base).lower()


_FRAME_RE = re.compile(r'File "([^"]*?(juggle_[^"/]*\.py))", line \d+, in (\S+)')


def innermost_app_frame(traceback: str | None) -> str | None:
    """Return the innermost (last) juggle frame as ``"juggle_x.py:func"`` (NO
    lineno) — dropping the lineno is what collapses line-drift. ``None`` when no
    juggle frame exists (the expected Class-B path)."""
    if not traceback:
        return None
    matches = _FRAME_RE.findall(traceback)
    if not matches:
        return None
    _full, fname, func = matches[-1]   # innermost (last) juggle frame, lineno dropped
    return f"{fname}:{func}"


def _first_line(text: str | None) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def group_key(row: dict) -> str:
    """Derived coarse grouping key (16-hex). HARD partitions: error_class,
    exc_type, entrypoint. The signal is the lineno-free innermost juggle frame,
    falling back to exc_type then the first traceback line, all normalized."""
    error_class = row.get("error_class") or ""
    exc_type = row.get("exc_type") or ""
    entrypoint = normalize_entrypoint(row.get("entrypoint"))
    signal = innermost_app_frame(row.get("traceback")) or exc_type or _first_line(row.get("traceback"))
    parts = [error_class, exc_type, entrypoint, normalize(signal)]
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]
