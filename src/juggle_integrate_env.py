"""Juggle — integrate test-env contract (2026-07-25 cyc_LI env-divergence incident).

`integrate` decides merge/no-merge by running the repo's configured `test_cmd`.
Before this module it ran with `env=None`: the suite inherited whatever
environment the CALLER happened to have — the watchdog daemon, an operator
shell, an agent pane, plus `~/.juggle/.env` when the caller reached integrate
through `juggle_cli` (which loads that file into os.environ at import). Those
differ, so the same commit could be green by hand and red in integrate. It cost
4 failed integrations of cyc_LI (8 commits blocked ~3 days): a test read the
ambient JUGGLE_MAX_THREADS=10 that every operator/agent shell exports and the
watchdog daemon does not.

THE CONTRACT — integrate CLEARS juggle's own namespace and passes EVERYTHING
else through.

  CONTROLLED (always cleared) — JUGGLE_*, _JUGGLE_*, CLAUDE_PLUGIN_DATA.
      These are juggle's own knobs. A test that reads one ambiently is
      non-hermetic by definition: it must pin what it needs, exactly as
      tests/conftest.py already pins JUGGLE_DB_PATH, _JUGGLE_CONFIG_PATH,
      JUGGLE_CONFIG_DIR, JUGGLE_SPOOL_DIR, JUGGLE_ORCHESTRATOR and
      JUGGLE_WATCHDOG_DISABLE_SPAWN. CLAUDE_PLUGIN_DATA joins them because no
      product code reads it (only the agent harness, which already UNSETS it —
      juggle_harness_defaults.env_unset), so clearing it here makes the agent
      and integrate paths consistent.

      CLEARED, never PINNED. Pinning a value (e.g. injecting
      JUGGLE_MAX_THREADS=10) would keep the ambient coupling alive and could
      turn today's false RED into a false GREEN — merging code that is broken
      on a fresh checkout. Removing the input is what makes the verdict
      deterministic; choosing a value for it is not.

  PASS-THROUGH (inherited verbatim) — everything else: HOME, PATH, TMPDIR,
      USER, SHELL, LANG/LC_*, TERM, UV_*, XDG_*, GIT_*, SSH_AUTH_SOCK, CI, ...
      A deny-list, NOT an allow-list: the set juggle owns is small and knowable;
      the set uv/git/pytest-xdist/the toolchain need is not, and an allow-list
      would be endless whack-a-mole whose failures are worse than the bug it
      fixes.

A deny-list also means this module never reasons about secrets: ~/.juggle/.env
credentials (OPENROUTER_KEY, VOYAGE_API_KEY, DEEPSEEK_API_KEY) flow through
untouched, and `format_env_report` only ever names CONTROLLED variables — so no
credential can reach an action item, an event, or the DB.

Reproduce integrate's exact env locally: `make test-integrate`
(scripts/run_integrate_env.py routes through this same `sanitized_env()`).

Owns ONLY the env contract — not the suite run, the retry, or the refusal
(juggle_integrate_fullsuite), and not the integrate pipeline
(juggle_cmd_integrate).
"""
from __future__ import annotations

import os
from collections.abc import Mapping

#: Variables integrate CONTROLS: juggle's own namespace, cleared before the suite runs.
CONTROLLED_PREFIXES: tuple[str, ...] = ("JUGGLE_", "_JUGGLE_")
CONTROLLED_NAMES: frozenset[str] = frozenset({"CLAUDE_PLUGIN_DATA"})

#: Report clip for a cleared variable's value (paths can be long; no secrets here).
_VALUE_CLIP = 60


def is_controlled(name: str) -> bool:
    """True if integrate CLEARS ``name`` before running the suite."""
    return name in CONTROLLED_NAMES or name.startswith(CONTROLLED_PREFIXES)


def dropped_overrides(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The CONTROLLED variables present in ``source`` (default ``os.environ``).

    This is exactly what integrate is about to clear — the diagnostic payload
    for a failing suite, and the only env content ever surfaced to a human.
    """
    src = os.environ if source is None else source
    return {k: v for k, v in src.items() if is_controlled(k)}


def sanitized_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``source`` (default ``os.environ``) with every CONTROLLED variable removed.

    Everything else is passed through verbatim — see the module docstring for
    why this is a deny-list and why nothing is ever pinned.
    """
    src = os.environ if source is None else source
    return {k: v for k, v in src.items() if not is_controlled(k)}


def _clip(value: str) -> str:
    return value[:_VALUE_CLIP] + "…" if len(value) > _VALUE_CLIP else value


def format_env_report(dropped: Mapping[str, str]) -> str:
    """One human line naming what integrate cleared — ALWAYS emitted on failure.

    Emitted even when nothing was dropped: silence is ambiguous, and the whole
    point of the 2026-07-25 incident is that an env-caused divergence must
    announce itself instead of looking like an ordinary red.
    """
    if not dropped:
        return (
            "env: sanitized — the caller had NO juggle overrides set, so the "
            "suite ran on config.json + DEFAULTS."
        )
    items = ", ".join(f"{k}={_clip(v)}" for k, v in sorted(dropped.items()))
    return (
        "env: sanitized — integrate CLEARED these caller overrides before "
        f"running the suite: {items}. If this suite passes in YOUR shell but "
        "fails here, a test is reading one of them ambiently instead of pinning "
        "it. Reproduce integrate's exact env with `make test-integrate`."
    )
