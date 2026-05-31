#!/usr/bin/env python3
"""OpenAI Codex CLI harness adapter — self-contained.

Codex differs from Claude Code in ways a launch string can't capture, so this
adapter encapsulates a different *strategy* for each surface:

  * launch       — NON-interactive one-shot ``codex exec`` (``interactive:
                   false``): each task spawns a fresh process that runs to
                   completion and exits. Simpler than a warm REPL — no readiness/
                   submission marker polling, no collapsed-paste retry dance. The
                   task prompt is fed by file via stdin — ``codex exec - <
                   prompt.txt`` (``prompt_arg = "- < {prompt_file}"``), the
                   documented stdin form that avoids ARG_MAX and the non-TTY-pipe
                   hang (openai/codex#20919). Model via ``-m {model}``.
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
  * context      — Codex auto-reads ``AGENTS.md``; the role anchor is INLINED
                   into the task prompt (``supports_hooks`` False → inherited
                   ``decorate_task`` prepends it). One-shot runs don't use
                   juggle's hooks anyway.

NOTE: Several Codex flag details vary by version. The conformance suite
(tests/test_harness_conformance.py) verifies the juggle-side contract; the
exact ``codex`` flags below should be confirmed against your installed CLI and
adjusted via the harness config (no code change needed).
Refs: https://developers.openai.com/codex/cli/reference ,
https://developers.openai.com/codex/config-reference
"""

from juggle_harness import HarnessAdapter, register_adapter

# Shipped defaults for a `"type": "codex"` harness. A deployment overrides any
# of these in agent.harnesses[<id>] without touching code.
CODEX_DEFAULTS: dict = {
    "type": "codex",
    # One-shot subcommand. `codex exec` runs non-interactively and exits.
    "command": "codex exec",
    # Non-interactive: spawn a fresh process per task (no warm REPL).
    "interactive": False,
    # Model flag + optional pinned model. Codex's model namespace is gpt-*, not
    # the orchestrator's sonnet/opus — set `model` (e.g. "gpt-5") to force the
    # Codex model regardless of the agent's configured model. Empty = use the
    # per-agent model as-is.
    "model_flag": "-m {model}",
    "model": "",
    # Arbitrary extra CLI flags appended verbatim, e.g. "-c model_reasoning_effort=high".
    "extra_flags": "",
    # The task prompt is read from the file via stdin. `-` is Codex's documented
    # "read prompt from stdin" sentinel; combined with the shell redirect this is
    # `codex exec - < prompt.txt`. Passing by file (not a positional arg) avoids
    # the OS ARG_MAX limit on large prompts and a known hang on non-TTY pipes
    # (openai/codex#20919). Ref: developers.openai.com/codex/cli/reference
    "prompt_arg": "- < {prompt_file}",
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
    # One-shot harnesses don't poll for REPL markers, but the conformance
    # contract still requires non-empty markers; keep minimal sentinels.
    "readiness_markers": ["codex"],
    "submission_markers": ["tokens used", "codex"],
    # Anchor inlined into the prompt (no juggle hooks in one-shot runs).
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
