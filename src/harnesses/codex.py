#!/usr/bin/env python3
"""OpenAI Codex CLI harness adapter — self-contained.

Codex differs from Claude Code in ways a launch string can't capture, so this
adapter encapsulates a different *strategy* for each surface:

  * launch       — interactive ``codex`` REPL (NOT ``codex exec``, which is
                   one-shot: juggle reuses warm panes across tasks, so it needs
                   the persistent REPL). Model via ``-m {model}``.
  * restriction  — Codex has no flat tool-deny list. Its safety primitive is a
                   sandbox + approval *mode*, so per-role limits are materialized
                   as ``-a <approval> -s <sandbox>`` flags. Defaults per role
                   (overridable in the harness config):
                       researcher → read-only      (no edits)
                       planner    → read-only
                       coder      → workspace-write (edit within the workspace)
                   Extra static restriction flags can be appended via the
                   harness ``restrictions_flag`` config key. Audit mode relaxes
                   the per-role sandbox to ``workspace-write`` so tool demand is
                   observable (mirrors Claude's deny-relaxation).
  * context      — Codex auto-reads ``AGENTS.md`` and (in recent builds) has a
                   hooks engine, but hook support is version-skewed across Codex
                   releases (the PreToolUse/UserPromptSubmit engine landed after
                   the legacy ``notify`` mechanism). So this adapter is
                   conservative: ``supports_hooks`` defaults to False and the
                   role anchor is INLINED into the task prompt (inherited
                   ``decorate_task``). A deployment on a hook-capable Codex can
                   set ``"supports_hooks": true`` in config to switch to hook
                   delivery.
  * capabilities — tmux markers for the Codex TUI (overridable in config).

NOTE: Several Codex flag/marker details vary by version. The conformance suite
(tests/test_harness_conformance.py) verifies the juggle-side contract; the
exact ``codex`` flags below should be confirmed against your installed CLI and
adjusted via the harness config (no code change needed for command/markers).
Refs: https://developers.openai.com/codex/cli/reference ,
https://developers.openai.com/codex/config-reference
"""

from juggle_harness import HarnessAdapter, register_adapter

# Shipped defaults for a `"type": "codex"` harness. A deployment overrides any
# of these in agent.harnesses[<id>] without touching code.
CODEX_DEFAULTS: dict = {
    "type": "codex",
    "command": "codex",
    "model_flag": "-m {model}",
    # Approval policy for non-interactive autonomy. "never" = no approval
    # prompts (juggle agents run unattended). Overridable.
    "approval_policy": "never",
    # Per-role sandbox mode → Codex `-s` value. read-only blocks file writes.
    "sandbox_by_role": {
        "researcher": "read-only",
        "planner": "read-only",
        "coder": "workspace-write",
    },
    # Sandbox used when no role-specific entry / unknown role.
    "sandbox_default": "read-only",
    # Audit mode sandbox — relaxed so the agent can exercise (and reveal demand
    # for) tools it would otherwise be blocked from.
    "sandbox_audit": "workspace-write",
    # Optional extra static restriction flags appended verbatim, e.g. a
    # `-c key=value` config override.
    "restrictions_flag": "",
    "env": {"JUGGLE_IS_AGENT": "1"},
    "env_unset": [],
    # Codex TUI markers — confirm against your installed version.
    "readiness_markers": ["Ctrl+C to exit", "› "],
    "submission_markers": ["Esc to interrupt", "working", "thinking"],
    # Conservative default: inline the anchor rather than rely on version-skewed
    # Codex hooks. Flip to true on a hook-capable Codex.
    "supports_hooks": False,
}


class CodexAdapter(HarnessAdapter):
    """Codex CLI adapter: per-role sandbox/approval flags instead of a deny list."""

    def _sandbox_for(self, role: str | None, audit: bool) -> str:
        if audit:
            return self._cfg.get("sandbox_audit", "workspace-write")
        by_role = self._cfg.get("sandbox_by_role") or {}
        return by_role.get(role) or self._cfg.get("sandbox_default", "read-only")

    def _restrictions_part(self, role: str | None, audit: bool) -> str:
        approval = self._cfg.get("approval_policy", "never")
        sandbox = self._sandbox_for(role, audit)
        parts = [f"-a {approval}", f"-s {sandbox}"]
        extra = (self._cfg.get("restrictions_flag") or "").strip()
        if extra:
            parts.append(extra)
        return " ".join(parts)


register_adapter("codex", CodexAdapter, defaults=CODEX_DEFAULTS)
