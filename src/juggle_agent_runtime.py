"""juggle_agent_runtime — pure cascade resolver for a dispatched agent's model +
reasoning effort (2026-06-30 agent model/effort config). No DB, no I/O.

Cascade, lowest→highest precedence:
  built-in default → agents.model/effort (global) → agents.by_role[role]
  → per-dispatch flag (--model/--effort).
"""
from __future__ import annotations

_BUILTIN_MODEL = "sonnet"  # historic default (was the CLI --model default)


def resolve_agent_runtime(
    role: str | None,
    *,
    model_flag: str | None = None,
    effort_flag: str | None = None,
    settings: dict | None = None,
) -> dict:
    """Resolve {"model": str, "effort": str | None} for a role via the cascade.

    A None/empty flag falls through to config; effort has no built-in default
    (None → the harness omits --effort and uses its own session default)."""
    agents = (settings or {}).get("agents") or {}
    by_role = (agents.get("by_role") or {}).get(role) or {}
    model = model_flag or by_role.get("model") or agents.get("model") or _BUILTIN_MODEL
    effort = effort_flag or by_role.get("effort") or agents.get("effort")
    return {"model": model, "effort": effort}
