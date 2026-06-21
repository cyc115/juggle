"""Stale-configuration detection for `juggle doctor` (pure logic).

Doctor runs this on EVERY invocation to catch config keys that have drifted
from the live schema. Two channels:

  * INERT keys — present in DEFAULTS (so the loader still accepts them) but no
    longer honored by any code path. The motivating case is the v1.80.0
    "always full suite" directive (commit b320ecd): `integrate.test_scope`,
    `integrate.core_tests`, and `integrate.quarantine_tests` are now ignored
    entirely — `juggle_cmd_integrate` always runs the full suite. A live
    config that still sets `quarantine_tests` SILENTLY masked tests before the
    directive and is pure dead weight after it. Inert keys are SAFE to prune.

  * UNKNOWN keys — keys absent from the DEFAULTS schema (typos, removed/renamed
    options). These are REPORT-ONLY: a user may keep intentional extra keys, so
    doctor never auto-removes them.

This module is intentionally dependency-free (just stdlib) so it can be unit
tested without touching the real ~/.juggle/config.json. Wiring lives in
`juggle_cmd_doctor.cmd_doctor`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

# ── inert registry ────────────────────────────────────────────────────────────
# Dotted path -> human note (each names the version that made it inert so a
# user reading doctor output understands WHY). All three integrate.* keys went
# inert with the v1.80.0 full-suite directive; they remain in DEFAULTS only so
# old config.json files don't crash the loader.
_FULL_SUITE_NOTE = (
    "ignored since v1.80.0 — integrate always runs the full suite "
    "(no test scoping / no quarantine deselect)"
)

INERT_KEYS: dict[str, str] = {
    "integrate.test_scope": _FULL_SUITE_NOTE,
    "integrate.core_tests": _FULL_SUITE_NOTE,
    "integrate.quarantine_tests": _FULL_SUITE_NOTE,
}

# ── free-form paths ───────────────────────────────────────────────────────────
# Dotted-path prefixes whose CHILDREN are user/role/harness/repo-named (or large
# free-form blobs). The unknown-key scan must not descend into these — every key
# beneath them is legitimately user-defined, not a typo.
FREEFORM_PATHS: set[str] = {
    "repos",
    "agent.harnesses",
    "agent.harness_by_role",
    "agent.role_context",
    "agent.settings_overlay_by_role",
    "task_templates",
}


def _path_present(cfg: dict, dotted: str) -> bool:
    """True if `dotted` resolves to an existing key in `cfg` (value may be null)."""
    node = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return isinstance(node, dict) and parts[-1] in node


def find_unknown_keys(cfg: dict, defaults: dict) -> list[str]:
    """Recursively collect dotted paths present in `cfg` but absent from `defaults`.

    Rules:
      * A path in INERT_KEYS is skipped (reported via the inert channel only —
        no double counting).
      * A path in FREEFORM_PATHS is not descended into (its children are all
        valid user-defined keys).
      * Recurse only where BOTH cfg[k] and defaults[k] are dicts. A value-type
        mismatch (dict where defaults has a scalar, etc.) is out of scope — only
        keys entirely absent from defaults are reported.

    Returns a sorted list of dotted paths.
    """
    unknown: list[str] = []

    def walk(c: dict, d: dict, prefix: str) -> None:
        for key, val in c.items():
            path = f"{prefix}.{key}" if prefix else key
            if path in INERT_KEYS:
                continue
            if key not in d:
                unknown.append(path)
                continue
            if path in FREEFORM_PATHS:
                # Known container; do not inspect its free-form children.
                continue
            if isinstance(val, dict) and isinstance(d[key], dict):
                walk(val, d[key], path)

    walk(cfg, defaults, "")
    return sorted(unknown)


def find_inert_keys(cfg: dict) -> list[tuple[str, str]]:
    """Return (dotted_path, note) for every INERT_KEYS path PRESENT in `cfg`.

    "Present" means the key exists at that dotted path even if its value is null
    or empty. Sorted by path.
    """
    found = [
        (path, note)
        for path, note in INERT_KEYS.items()
        if _path_present(cfg, path)
    ]
    return sorted(found, key=lambda pair: pair[0])


@dataclass
class StaleConfigReport:
    """Result of scanning a config for stale keys.

    inert   — (path, note) pairs for keys the code no longer honors (prunable).
    unknown — dotted paths absent from the schema (report-only).
    """

    inert: list[tuple[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.inert) or bool(self.unknown)

    @property
    def prunable_paths(self) -> list[str]:
        """Only the inert paths — the safe-to-auto-remove set."""
        return [path for path, _note in self.inert]


def analyze_config(cfg: dict, defaults: dict) -> StaleConfigReport:
    """Scan `cfg` against `defaults`, separating inert and unknown findings."""
    return StaleConfigReport(
        inert=find_inert_keys(cfg),
        unknown=find_unknown_keys(cfg, defaults),
    )


def format_report(report: StaleConfigReport) -> list[str]:
    """Human-readable lines for doctor output (one finding per line)."""
    if not report.has_findings:
        return ["stale config: none — config is clean"]
    total = len(report.inert) + len(report.unknown)
    lines = [f"stale config: {total} issue(s)"]
    for path, note in report.inert:
        lines.append(f"  - INERT  {path}: {note}")
    for path in report.unknown:
        lines.append(
            f"  - UNKNOWN {path}: not in current schema "
            "(removed/renamed/typo?) — report only"
        )
    return lines


def prune_config(cfg: dict, paths: list[str]) -> tuple[dict, list[str]]:
    """Return a deep copy of `cfg` with each PRESENT dotted path removed.

    Does NOT mutate the input. Missing paths are silently skipped. Now-empty
    parent dicts are left in place ({}) to avoid touching sibling structure.
    Returns (new_cfg, sorted list of actually-removed paths).
    """
    new_cfg = copy.deepcopy(cfg)
    removed: list[str] = []
    for dotted in paths:
        if not _path_present(new_cfg, dotted):
            continue
        parts = dotted.split(".")
        node = new_cfg
        for part in parts[:-1]:
            node = node[part]
        del node[parts[-1]]
        removed.append(dotted)
    return new_cfg, sorted(removed)
