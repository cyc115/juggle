"""Comprehensive lint: no removed legacy CLI name in a live invocation position.

P9 removed the legacy flat command names (``complete-agent``, ``create-thread``,
``notify``, …) — only the resource-verb grammar resolves; a legacy name exits 2.
M2's verify-gate grepped only 8 of the ~50 removed names, so residual call sites
(e.g. ``commands/open.md`` → ``open-in-editor``) slipped through.

This gate scans EVERY prompt/skill/script surface (commands/, skills/, scripts/)
for the FULL removed-legacy-name set (tests/data/legacy_names.txt) in CLI-command
position and fails if any remains. A negated invocation (``! uv run … <name>``)
is exempt: that is a removal-ASSERTION (the name must be rejected), not a live
call. Verbs that survived NAMESPACED (``agent send-task``, ``thread
set-summarized-count``, …) are not flagged — they are not in command position.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_LEGACY = set((_ROOT / "tests" / "data" / "legacy_names.txt").read_text().split())
_SCAN_DIRS = ("commands", "skills", "scripts")

# A juggle entrypoint (juggle_cli.py or the `juggle` binary), optional flags, then
# the command token. The negative-lookbehind keeps `juggle` from matching inside a
# path/identifier (e.g. juggle_cli, /juggle/).
_CMD_RE = re.compile(
    r"(?:juggle_cli\.py|(?<![\w./-])juggle)\s+(?:-\S+\s+)*([a-z][a-z0-9][a-z0-9-]*)"
)
# Shell command separators — a `!`-negated SEGMENT asserts the name is rejected.
_SEG_RE = re.compile(r"&&|\|\||;|\|")


def _scan_text(text: str):
    """Return (lineno, legacy_name, line) for each live legacy invocation."""
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for seg in _SEG_RE.split(line):
            if seg.lstrip().startswith("!"):
                continue  # negated → removal assertion, not a live call
            for m in _CMD_RE.finditer(seg):
                if m.group(1) in _LEGACY:
                    hits.append((lineno, m.group(1), line.strip()))
    return hits


def _all_residuals():
    residuals = []
    for d in _SCAN_DIRS:
        for f in sorted((_ROOT / d).rglob("*")):
            if not f.is_file():
                continue
            try:
                text = f.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, name, line in _scan_text(text):
                residuals.append(f"{f.relative_to(_ROOT)}:{lineno}  [{name}]  {line}")
    return residuals


def test_legacy_names_txt_is_populated():
    # The gate is only as good as its name list — fail loud if it empties out.
    assert len(_LEGACY) >= 40, f"legacy_names.txt unexpectedly small: {len(_LEGACY)}"


def test_no_removed_legacy_name_in_cli_invocation_position():
    residuals = _all_residuals()
    assert not residuals, (
        "Removed legacy CLI names still invoked (migrate to resource-verb grammar):\n"
        + "\n".join(residuals)
    )


def test_negated_legacy_invocation_is_exempt():
    # A `! … <legacy>` rejection assertion (p9_verify scripts) must NOT be flagged.
    assert _scan_text("! uv run src/juggle_cli.py complete-agent X 'y' 2>/dev/null") == []
    # …but a plain live invocation of the same name IS flagged.
    assert _scan_text("uv run src/juggle_cli.py complete-agent X 'y'")
