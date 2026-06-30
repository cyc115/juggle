"""juggle_cli_aliases — legacy-flat-name → resource-verb argv shim (P9 G1).

Pre-parse argv rewrite (spec §4): the G1 grammar rename moved every legacy flat
command (``complete-agent``, ``create-thread``, …) to a ``<resource> <verb>`` form.
To keep zero-breakage (spec §5 stage a — "all legacy names still work"), main()
rewrites ``argv`` through ``rewrite_argv`` BEFORE argparse runs, mapping each legacy
name to its canonical ``[resource, verb]`` tokens (positionals + flags ride along).

The alias map is DERIVED from ``COMMANDS.aliases`` plus the entry-module verb
aliases (vault-path / vault-name / open-in-editor, which are registered
imperatively, not in COMMANDS).

G1 TRANSITIONAL: A1 will extend this module with the ``ALIASES`` constant, the
``aliases --json`` command, a coverage test (set(ALIASES) ⊇ all legacy names), and
the silent→warn deprecation flip (D1).
"""

from __future__ import annotations

# Legacy flat names for the entry-module verbs (registered imperatively in
# juggle_cli.build_cli_parser, so NOT present in COMMANDS.aliases).
_ENTRY_VERB_ALIASES: dict[str, list[str]] = {
    "vault-path": ["vault", "path"],
    "vault-name": ["vault", "name"],
    "open-in-editor": ["file", "open"],
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


def rewrite_argv(argv: list[str]) -> list[str]:
    """Splice a legacy flat command name into its canonical [resource, verb] form.

    Only argv[1] (the command token) is considered; positionals/flags ride along.
    Skips when argv is ALREADY canonical — guards the one legacy name that collides
    with its resource group (``research`` is both the ``research run`` alias AND the
    resource), so ``research run …`` is left intact.
    """
    if len(argv) >= 2 and not argv[1].startswith("-"):
        target = legacy_alias_map().get(argv[1])
        if target is not None and argv[1:1 + len(target)] != target:
            return [argv[0], *target, *argv[2:]]
    return argv
