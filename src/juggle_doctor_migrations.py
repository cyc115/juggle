"""Pure config-migration helpers for `juggle doctor`.

Extracted from juggle_cmd_doctor (LOC/architecture gate) so the config-schema
migrations live in one small, single-purpose module. Each helper is pure
(dict-in → dict-out + change notes) and idempotent. juggle_cmd_doctor imports
``_migrate_config`` back for its historical call/patch surface.
"""
from __future__ import annotations

import copy

_STABLE_READY_MARKER = "shift+tab to cycle"


def _migrate_readiness_markers(cfg: dict, changes: list[str]) -> None:
    """Defect E (2026-07-01): upgrade a stale claude readiness_markers list.

    The old markers ("bypass permissions on" / "/effort") pinned transient or
    mode-specific chrome; juggle spawns in accept-edits mode, so a settled ready
    pane matched neither and fresh spawns hung until the readiness timeout.
    Additively prepend the stable structural marker. Idempotent — never removes
    a user's own markers. Mutates ``cfg`` in place.
    """
    agent = cfg.get("agent")
    claude = agent.get("harnesses", {}).get("claude") if isinstance(agent, dict) else None
    if not isinstance(claude, dict):
        return
    markers = claude.get("readiness_markers")
    if isinstance(markers, list) and _STABLE_READY_MARKER not in markers:
        claude["readiness_markers"] = [_STABLE_READY_MARKER] + markers
        changes.append(
            f"agent.harnesses.claude.readiness_markers += {_STABLE_READY_MARKER!r} "
            "(defect E: stable spawn-readiness marker)"
        )


def _migrate_domains(cfg: dict, changes: list[str]) -> None:
    """Rewrite the pre-1.21.0 ``domains`` block to ``paths.*`` and drop it."""
    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        return

    paths = cfg.setdefault("paths", {})

    if "vault" not in paths:
        for entry in domains.get("initial_domain_paths") or []:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) >= 2
                and entry[1] == "vault"
            ):
                paths["vault"] = entry[0]
                changes.append(
                    f"paths.vault = {entry[0]} (migrated from domains.initial_domain_paths)"
                )
                break

    if "vault_name" not in paths:
        legacy_name = domains.get("vault_name", "")
        if legacy_name:
            paths["vault_name"] = legacy_name
            changes.append(
                f"paths.vault_name = {legacy_name} (migrated from domains.vault_name)"
            )

    del cfg["domains"]
    changes.append("removed obsolete domains block")


def _migrate_config(cfg: dict) -> tuple[dict, list[str]]:
    """Rewrite a config dict to the current schema. Returns (new_cfg, changes)."""
    cfg = copy.deepcopy(cfg)
    changes: list[str] = []
    _migrate_readiness_markers(cfg, changes)
    _migrate_domains(cfg, changes)
    return cfg, changes
