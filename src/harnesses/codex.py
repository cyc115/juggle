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

class CodexAdapter(HarnessAdapter):
    """Codex CLI adapter: per-role sandbox/approval flags instead of a deny list.

    The shipped config (one-shot ``codex exec``, ``sandbox_by_role``, etc.) lives
    in ``juggle_settings.DEFAULTS["agent"]["harnesses"]["codex"]`` — the single
    source of truth for harness defaults. This module holds only the *logic* that
    a flat config dict can't express: translating a role into a sandbox mode.
    """

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


register_adapter("codex", CodexAdapter)
