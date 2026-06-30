"""juggle_cli_aliases — legacy-flat-name → resource-verb argv shim (P9 G1).

Pre-parse argv rewrite (spec §4): the G1 grammar rename moved every legacy flat
command (``complete-agent``, ``create-thread``, …) to a ``<resource> <verb>`` form.
To keep zero-breakage (spec §5 stage a — "all legacy names still work"), main()
rewrites ``argv`` through ``rewrite_argv`` BEFORE argparse runs, mapping each legacy
name to its canonical ``[resource, verb]`` tokens (positionals + flags ride along).

The alias map is DERIVED from ``COMMANDS.aliases`` plus the entry-module verb
aliases (vault-path / vault-name / open-in-editor, which are registered
imperatively, not in COMMANDS).

A1 formalized this: the materialized ``ALIASES`` constant + a ``warn`` flag on
``rewrite_argv`` (default False = silent — spec §5 stage a). A2 adds the
``aliases --json`` command + the ⊇-coverage test; D1 flips the call site to
``warn=True`` (stderr-only deprecation notices).
"""

from __future__ import annotations

import sys

# Legacy flat names for the entry-module verbs (registered imperatively in
# juggle_cli.build_cli_parser, so NOT present in COMMANDS.aliases).
_ENTRY_VERB_ALIASES: dict[str, list[str]] = {
    "vault-path": ["vault", "path"],
    "vault-name": ["vault", "name"],
    "open-in-editor": ["file", "open"],
    # P9 G2: `project-graph load …` → `graph load …` (single-token rewrite; the
    # `load`/flags ride along). project-graph was a top-level group, now folded.
    "project-graph": ["graph"],
}


def legacy_alias_map() -> dict[str, list[str]]:
    """{legacy-flat-name: [resource, verb]} from COMMANDS.aliases + entry verbs."""
    from juggle_cli_commands import COMMANDS

    mapping: dict[str, list[str]] = {}
    for c in COMMANDS:
        target = [c.verb] if c.resource is None else [c.resource, c.verb]
        for alias in c.aliases:
            mapping[alias] = target
    for alias, target in _ENTRY_VERB_ALIASES.items():
        mapping.setdefault(alias, target)
    return mapping


# Materialized once at import (the canonical alias→[resource, verb] table). A2's
# coverage test asserts set(ALIASES) ⊇ every legacy name; D1/X2 read it too.
ALIASES: dict[str, list[str]] = legacy_alias_map()


def rewrite_argv(argv: list[str], *, warn: bool = False) -> list[str]:
    """Splice a legacy flat command name into its canonical [resource, verb] form.

    Only argv[1] (the command token) is considered; positionals/flags ride along.
    Skips when argv is ALREADY canonical — guards the one legacy name that collides
    with its resource group (``research`` is both the ``research run`` alias AND the
    resource), so ``research run …`` is left intact.

    ``warn`` (default False = silent, spec §5 stage a) emits a one-line deprecation
    notice to STDERR only (never stdout — agents parse stdout/JSON). D1 flips the
    main() call site to ``warn=True``.
    """
    if len(argv) >= 2 and not argv[1].startswith("-"):
        target = ALIASES.get(argv[1])
        if target is not None and argv[1:1 + len(target)] != target:
            if warn:
                print(
                    f"juggle: '{argv[1]}' is deprecated; use "
                    f"'juggle {' '.join(target)}'",
                    file=sys.stderr,
                )
            return [argv[0], *target, *argv[2:]]
    return argv
