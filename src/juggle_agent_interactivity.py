"""juggle_agent_interactivity — one-place resolution of agent harness interactivity.

Extracted from juggle_watchdog (2026-07-07, completed-agents-leak) so the watchdog
tick, the stall detector, the completed-agent reaper, and the pool inspector all
share ONE definition instead of importing it from the 1000-line watchdog hub. Pure
classification over the agent's PERSISTED harness config (never the current global
default), so a recycled claude pane still reads as interactive after a config switch.
"""
from __future__ import annotations


def agent_is_non_interactive(agent: dict) -> bool:
    """Return True if the agent's persisted harness is non-interactive (one-shot).

    Resolves the adapter from the **persisted** harness id (so a recycled claude
    pane still shows as interactive even if current config says reasonix).
    """
    try:
        harness_id = agent.get("harness")
        if not harness_id:
            return False
        # Resolve using the agent's OWN harness config, not the current global default.
        from juggle_settings import get_settings
        agent_cfg = get_settings().get("agent", {})
        harnesses = agent_cfg.get("harnesses") or {}
        hcfg = harnesses.get(harness_id)
        if hcfg is not None:
            # Use the adapter type from config to determine interactivity
            is_interactive = hcfg.get("interactive", True)
            return not is_interactive
        return False
    except Exception:
        return False
