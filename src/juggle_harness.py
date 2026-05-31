#!/usr/bin/env python3
"""Pluggable harness adapters for launching juggle sub-agents.

Juggle spawns each background agent as a full interactive CLI process inside a
tmux pane (see ``juggle_tmux.JuggleTmuxManager``). Historically that process was
hard-wired to Claude Code: the launch command, the per-role tool-restriction
mechanism (``--settings <overlay.json>``) and the tmux readiness / submission
markers were all Claude-specific and lived inline in ``start_claude_in_pane``.

This module factors every harness-specific decision behind a ``HarnessAdapter``
so a deployment can point juggle at a different CLI (Codex, or any future
harness) **purely through config**. Two layers, per the project's
"code over prompts / reuse before new abstractions" philosophy:

  * Built-in adapters encode behaviour that needs real logic. ``ClaudeCodeAdapter``
    generates Claude's additive settings overlay via ``juggle_agent_settings``.
  * ``TemplateHarnessAdapter`` is fully config-driven: a harness defined only by a
    command template + markers in ``config.json`` works with **no Python**. This
    is the "bring your own harness" path (codex, reasonix, …).

Selection is configurable:
  * ``agent.harness``              — global default harness id.
  * ``agent.harness_by_role[role]`` — optional per-role override.
  * ``agent.harnesses[id]``        — the harness definitions (see schema below).

A harness definition is a dict with keys:
  * ``type``                — ``"claude"`` (built-in overlay) or ``"template"``.
  * ``command``             — launch command (falls back to ``agent.claude_launch_command``).
  * ``model_flag``          — format string applied when a model is given, e.g. ``"--model {model}"``.
  * ``restrictions_flag``   — (template only) static flag fragment for tool restriction.
  * ``env``                 — dict of env vars to export before the command.
  * ``env_unset``           — list of env vars to ``env -u`` (scrub) before the command.
  * ``readiness_markers``   — substrings that signal the REPL is ready for paste.
  * ``submission_markers``  — substrings that signal a pasted prompt was submitted.
  * ``supports_hooks``      — whether the harness runs juggle's Claude Code hooks.
                              When false, the role anchor is inlined into the task
                              prompt instead of injected via UserPromptSubmit.

Backward compatibility: if ``agent.harnesses`` is absent (or omits the selected
id) a built-in "claude" harness is synthesised from the legacy
``agent.claude_launch_command`` so existing configs keep working unchanged.
"""

import shlex

from juggle_settings import get_settings

# Built-in Claude Code defaults. Doubles as the synthesised fallback when a
# config has no `harnesses` block (older configs) — keeps behaviour identical.
_CLAUDE_DEFAULTS: dict = {
    "type": "claude",
    "model_flag": "--model {model}",
    "env": {"JUGGLE_IS_AGENT": "1"},
    "env_unset": ["CLAUDE_PLUGIN_DATA"],
    "readiness_markers": ["bypass permissions on", "/effort"],
    "submission_markers": ["esc to interrupt", "✻", "✶"],
    "supports_hooks": True,
}


def _env_prefix(env: dict | None, env_unset, role: str | None, audit: bool) -> str:
    """Build the shared ``env ...`` command prefix.

    Always exports ``JUGGLE_IS_AGENT=1`` (plus ``JUGGLE_AGENT_ROLE`` /
    ``JUGGLE_AGENT_AUDIT`` when applicable) so juggle's hooks and telemetry can
    identify the agent regardless of harness. Harness-specific additions come
    from ``env`` (exported) and ``env_unset`` (scrubbed via ``-u``).

    Insertion order matches the legacy hand-built command exactly:
    ``env -u <unset...> JUGGLE_IS_AGENT=1 [JUGGLE_AGENT_ROLE=..] [JUGGLE_AGENT_AUDIT=1] <set...>``.
    """
    parts = ["env"]
    for name in env_unset or ():
        parts.append(f"-u {name}")
    merged: dict = {"JUGGLE_IS_AGENT": "1"}
    if role:
        merged["JUGGLE_AGENT_ROLE"] = role
    if audit:
        merged["JUGGLE_AGENT_AUDIT"] = "1"
    # Any extra harness env (after the juggle markers); JUGGLE_IS_AGENT already set.
    for k, v in (env or {}).items():
        if k == "JUGGLE_IS_AGENT":
            continue
        merged[k] = v
    for k, v in merged.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


class HarnessAdapter:
    """Base adapter — config-driven command, markers and task decoration.

    Subclasses override ``_restrictions_part`` to apply a harness's per-role
    tool-restriction mechanism. The base class itself is a usable, purely
    config-driven adapter (no restrictions) — ``TemplateHarnessAdapter`` is a
    thin named alias of it for readability in config (``"type": "template"``).
    """

    def __init__(self, harness_id: str, harness_cfg: dict, agent_cfg: dict):
        self.id = harness_id
        self._cfg = harness_cfg or {}
        self._agent_cfg = agent_cfg or {}

    # -- capabilities -------------------------------------------------------
    @property
    def supports_hooks(self) -> bool:
        return bool(self._cfg.get("supports_hooks", False))

    def readiness_markers(self) -> tuple:
        return tuple(self._cfg.get("readiness_markers") or ())

    def submission_markers(self) -> tuple:
        return tuple(self._cfg.get("submission_markers") or ())

    # -- command assembly ---------------------------------------------------
    def _command(self) -> str:
        """Launch command: explicit per-harness, else legacy key, else the id."""
        return (
            self._cfg.get("command")
            or self._agent_cfg.get("claude_launch_command")
            or self.id
        )

    def _model_part(self, model: str | None) -> str:
        if not model:
            return ""
        return self._cfg.get("model_flag", "--model {model}").format(model=model)

    def _restrictions_part(self, role: str | None, audit: bool) -> str:
        """Flag fragment that applies per-role tool restrictions.

        Base / template behaviour: emit the static ``restrictions_flag`` from
        config (empty by default). Override in subclasses that generate files.
        """
        return self._cfg.get("restrictions_flag", "") or ""

    def build_launch_command(
        self, role: str | None = None, model: str | None = None, audit: bool = False
    ) -> str:
        """Return the full shell command string to paste into a fresh pane."""
        parts = [
            _env_prefix(self._cfg.get("env"), self._cfg.get("env_unset"), role, audit),
            self._command(),
        ]
        model_part = self._model_part(model)
        if model_part:
            parts.append(model_part)
        restrictions = self._restrictions_part(role, audit)
        if restrictions:
            parts.append(restrictions)
        return " ".join(parts)

    # -- task decoration ----------------------------------------------------
    def decorate_task(self, role: str | None, prompt: str) -> str:
        """Adjust a task prompt before it is pasted into the agent.

        Claude runs juggle's ``UserPromptSubmit`` hook, which injects the role
        anchor, so the default leaves the prompt untouched. Harnesses without
        juggle hooks get the anchor prepended here instead, so the agent still
        learns its role + completion command.
        """
        if self.supports_hooks:
            return prompt
        try:
            from juggle_context import render_agent_role_anchor_for

            anchor = render_agent_role_anchor_for(role)
        except Exception:
            anchor = ""
        return f"{anchor}\n\n{prompt}" if anchor else prompt


class TemplateHarnessAdapter(HarnessAdapter):
    """Fully config-driven adapter (the "bring your own harness" path)."""


class ClaudeCodeAdapter(HarnessAdapter):
    """Built-in Claude Code adapter: per-role denies via a ``--settings`` overlay."""

    def _restrictions_part(self, role: str | None, audit: bool) -> str:
        # Per-role denied tools (and any future per-role settings) are written to
        # a settings overlay file and passed via `--settings <path>` — one short,
        # fixed token that pastes reliably into the pane. Imported at call time so
        # tests patching `juggle_agent_settings.write_agent_overlay` take effect.
        import juggle_agent_settings as _jas

        overlay_path = _jas.write_agent_overlay(role)
        return "--settings " + shlex.quote(str(overlay_path))


_ADAPTERS = {
    "claude": ClaudeCodeAdapter,
    "template": TemplateHarnessAdapter,
}


def get_adapter(role: str | None = None, agent_cfg: dict | None = None) -> HarnessAdapter:
    """Resolve the harness adapter for ``role``.

    Selection precedence: ``agent.harness_by_role[role]`` → ``agent.harness`` →
    ``"claude"``. Pass ``agent_cfg`` to inject settings (used by callers that
    monkeypatch their own ``get_settings``); otherwise it is read globally.
    """
    if agent_cfg is None:
        agent_cfg = get_settings().get("agent", {})

    harnesses = agent_cfg.get("harnesses") or {}
    hid = (
        (agent_cfg.get("harness_by_role") or {}).get(role)
        or agent_cfg.get("harness")
        or "claude"
    )
    hcfg = harnesses.get(hid)
    if hcfg is None:
        # Unknown / missing harness id → synthesise the built-in claude harness
        # so legacy configs (no `harnesses` block) keep working unchanged.
        hid, hcfg = "claude", dict(_CLAUDE_DEFAULTS)

    cls = _ADAPTERS.get(hcfg.get("type", "template"), TemplateHarnessAdapter)
    return cls(hid, hcfg, agent_cfg)
