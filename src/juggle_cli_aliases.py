"""juggle_cli_aliases — legacy-flat-name alias layer (P9; REMOVED in X2).

History (spec §5): the G1 grammar rename moved every legacy flat command
(``complete-agent``, ``create-thread``, …) to a ``<resource> <verb>`` form. A1-D1
kept a backward-compat shim — ``main()`` rewrote ``argv`` through ``rewrite_argv``
BEFORE argparse ran (silent in stage a, deprecation-warning in stage b/D1).

P9 X2 (2026-06-30, user-approved IRREVERSIBLE removal — spec §5 stage d): the alias
layer is GONE. ``ALIASES`` is now an empty map, so NO legacy flat name is rewritten —
invoking one is an unknown argparse choice and exits 2. Only the NEW resource-verb
grammar resolves. ``rewrite_argv`` is retained as an inert pass-through (ALIASES is
empty → it returns argv unchanged) so the D1 ``main()`` call site keeps a stable
signature; the deprecation-warn branch is now dead. ``aliases --json`` emits ``{}``.
"""

from __future__ import annotations

import sys

# P9 X2 (2026-06-30): the legacy alias→[resource, verb] table is now EMPTY. The
# entries were previously DERIVED from COMMANDS.aliases + the entry-module verb
# aliases; that derivation is deleted. ``aliases --json`` reads this and emits {}.
ALIASES: dict[str, list[str]] = {}


def rewrite_argv(argv: list[str], *, warn: bool = False) -> list[str]:
    """Inert since X2 — returns ``argv`` unchanged.

    Formerly spliced a legacy flat command name into its canonical
    ``[resource, verb]`` form. With ``ALIASES`` now empty (X2 removal) the lookup
    never matches, so this is a pass-through. Kept so the ``main()`` call site
    (``_rewrite_legacy_argv(..., warn=True)``) needs no change; the ``warn`` branch
    is now unreachable (no legacy name resolves to deprecate).
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


def cmd_aliases(args) -> None:
    """`juggle aliases [--json]` — dump the legacy→canonical alias map.

    Post-X2 the map is empty: ``--json`` emits ``{}`` and the human form prints
    nothing. Retained as the agent-verifiable primitive that pins the removal.
    """
    import json

    if getattr(args, "json_out", False):
        print(json.dumps(ALIASES, sort_keys=True))
    else:
        for name in sorted(ALIASES):
            print(f"{name} -> juggle {' '.join(ALIASES[name])}")
