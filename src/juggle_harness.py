#!/usr/bin/env python3
"""Pluggable harness adapters for launching juggle sub-agents — framework.

Juggle spawns each background agent as a CLI process in a tmux pane
(``juggle_tmux.JuggleTmuxManager``). This module is the framework: the
``HarnessAdapter`` contract, the registry, and ``get_adapter`` resolution. Each
concrete harness is self-contained in its own module under ``src/harnesses/``
and owns, in one place: launch (``build_launch_command``), per-role restriction
(``_restrictions_part``), context delivery (``decorate_task``), and capabilities
(``supports_hooks`` / ``is_interactive`` / tmux markers).

Selection (config under ``agent`` in ``~/.juggle/config.json``):
``harness_by_role[role]`` → ``harness`` → ``"claude"``; definitions live in
``harnesses[id]`` (schema: docs/harness-adapters.md). A config with no
``harnesses`` block falls back to the built-in claude harness, so older configs
are unchanged.
"""

from juggle_settings import DEFAULTS, get_settings

# Concrete adapters self-register here, from their own modules — imported at the
# bottom of this file so a plain ``import juggle_harness`` wires them up.
_ADAPTERS: dict = {}


def register_adapter(type_name: str, cls) -> None:
    """Register an adapter class under its ``type`` name (called at import time
    from each ``harnesses/<name>.py``)."""
    _ADAPTERS[type_name] = cls


def _env_prefix(env: dict | None, env_unset, role: str | None, audit: bool) -> str:
    """Build the shared ``env ...`` command prefix.

    Two layers: juggle exports its identity vars (``JUGGLE_IS_AGENT=1`` plus
    ``JUGGLE_AGENT_ROLE`` / ``JUGGLE_AGENT_AUDIT`` when applicable) so hooks and
    telemetry can attribute the agent regardless of harness; then the harness's
    own ``env`` dict is applied on top — it can set OR override **any** variable
    for the launched process (overriding the inherited environment, and the
    juggle defaults too if a harness deliberately lists them). ``env_unset``
    scrubs inherited vars via ``-u``. So the environment is fully overridable
    per harness while the defaults stay correct for harnesses that don't touch
    them.

    Order: ``env -u <unset...> JUGGLE_IS_AGENT=1 [JUGGLE_AGENT_ROLE=..]
    [JUGGLE_AGENT_AUDIT=1] <harness env...>``.
    """
    parts = ["env"]
    for name in env_unset or ():
        parts.append(f"-u {name}")
    merged: dict = {"JUGGLE_IS_AGENT": "1"}
    if role:
        merged["JUGGLE_AGENT_ROLE"] = role
    if audit:
        merged["JUGGLE_AGENT_AUDIT"] = "1"
    merged.update(env or {})  # harness env is authoritative — fully overridable
    for k, v in merged.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


class HarnessAdapter:
    """Base adapter — config-driven command, markers and task decoration.

    Subclasses override ``_restrictions_part`` to apply a harness's per-role
    tool-restriction mechanism (and may override ``decorate_task`` for a
    harness-specific context-delivery strategy). The base class itself is a
    usable, purely config-driven adapter (no restrictions) —
    ``TemplateHarnessAdapter`` is a thin named alias of it for readability in
    config (``"type": "template"``).
    """

    def __init__(self, harness_id: str, harness_cfg: dict, agent_cfg: dict):
        self.id = harness_id
        self._cfg = harness_cfg or {}
        self._agent_cfg = agent_cfg or {}

    # -- capabilities -------------------------------------------------------
    @property
    def supports_hooks(self) -> bool:
        return bool(self._cfg.get("supports_hooks", False))

    @property
    def external_restriction(self) -> bool:
        """True when per-role tool restriction is delegated to the harness's own
        config file (e.g. reasonix's ``reasonix.toml``) rather than materialized
        by juggle as command flags or a ``--settings`` overlay. A deliberate,
        declared opt-out of juggle-managed restriction — never a silent drop."""
        return bool(self._cfg.get("external_restriction", False))

    @property
    def is_interactive(self) -> bool:
        """Whether the harness runs as a long-lived interactive REPL pane.

        Interactive (Claude Code, default): launch the REPL once, then paste each
        task into the warm pane and watch for tmux readiness/submission markers.

        Non-interactive / one-shot (``"interactive": false``): each task spawns a
        fresh ``<command> ... <prompt>`` process that runs to completion and
        exits. Simpler — no warm-pane reuse, no readiness/submission marker
        polling — and the natural fit for ``codex exec`` / ``reasonix run``-style
        CLIs. The prompt is passed via a file argument (``prompt_arg``) to avoid
        shell-escaping a multi-line task.
        """
        return bool(self._cfg.get("interactive", True))

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
        # A harness may pin its own model via the config ``model`` key, which
        # overrides the per-agent model passed in. Useful when the harness's
        # model namespace differs from the orchestrator's (e.g. Codex uses
        # ``gpt-*`` names, not ``sonnet``/``opus``). The flag itself is also
        # configurable via ``model_flag`` (Codex uses ``-m {model}``).
        model = self._cfg.get("model") or model
        if not model:
            return ""
        return self._cfg.get("model_flag", "--model {model}").format(model=model)

    def _effort_part(self, effort: str | None) -> str:
        """Reasoning-effort flag fragment (2026-06-30 agent model/effort config).

        Mirrors ``_model_part``: the flag template is configurable via
        ``effort_flag`` (default ``--effort {effort}``, Claude Code's launch flag).
        Empty/None effort → no flag (harness uses its own session default)."""
        if not effort:
            return ""
        return self._cfg.get("effort_flag", "--effort {effort}").format(effort=effort)

    def _restrictions_part(self, role: str | None, audit: bool) -> str:
        """Flag fragment that applies per-role tool restrictions.

        Base / template behaviour: emit the static ``restrictions_flag`` from
        config (empty by default). Override in subclasses that generate files
        (Claude) or emit harness-native flags (Codex).
        """
        return self._cfg.get("restrictions_flag", "") or ""

    def build_launch_command(
        self, role: str | None = None, model: str | None = None,
        audit: bool = False, effort: str | None = None,
    ) -> str:
        """Return the full shell command string to paste into a fresh pane."""
        parts = [
            _env_prefix(self._cfg.get("env"), self._cfg.get("env_unset"), role, audit),
            self._command(),
        ]
        model_part = self._model_part(model)
        if model_part:
            parts.append(model_part)
        effort_part = self._effort_part(effort)
        if effort_part:
            parts.append(effort_part)
        restrictions = self._restrictions_part(role, audit)
        if restrictions:
            parts.append(restrictions)
        # Arbitrary extra CLI flags, appended verbatim — the config escape hatch
        # for harness flags juggle doesn't model explicitly (e.g. `-c key=value`).
        extra = (self._cfg.get("extra_flags") or "").strip()
        if extra:
            parts.append(extra)
        return " ".join(parts)

    def build_task_command(
        self,
        prompt_file: str,
        role: str | None = None,
        model: str | None = None,
        audit: bool = False,
        effort: str | None = None,
    ) -> str:
        """Return a one-shot shell command that runs ``prompt_file`` to completion.

        Only meaningful for non-interactive harnesses. It is the launch command
        plus a reference to the prompt file via ``prompt_arg`` (a format string
        taking ``{prompt_file}``). The prompt is passed by **file**, not inlined
        on the command line — the default ``"< {prompt_file}"`` redirects the
        file to the process's stdin. This avoids the OS ``ARG_MAX`` ceiling on a
        large prompt and needs no shell-escaping of multi-line content. Harnesses
        that want an explicit stdin sentinel or a file flag override ``prompt_arg``
        (Codex uses ``"- < {prompt_file}"``).
        """
        launch = self.build_launch_command(role=role, model=model, audit=audit, effort=effort)
        prompt_arg = self._cfg.get("prompt_arg", "< {prompt_file}")
        return f"{launch} {prompt_arg.format(prompt_file=prompt_file)}"

    # -- task decoration (context delivery) ---------------------------------
    def decorate_task(self, role: str | None, prompt: str) -> str:
        """Adjust a task prompt before it is pasted into the agent.

        Default strategy: hook-capable harnesses inject the role anchor via
        their UserPromptSubmit-equivalent hook, so the prompt is left untouched;
        harnesses without juggle hooks get the anchor prepended here so the
        agent still learns its role + completion command. A harness with a
        different mechanism (e.g. writing AGENTS.md) may override this.
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


# The template type needs no dedicated module — register it here.
register_adapter("template", TemplateHarnessAdapter)


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
        # Unknown / missing harness id → fall back to the shipped claude config
        # so a partial agent_cfg (no `harnesses` block) keeps working unchanged.
        hid, hcfg = "claude", dict(DEFAULTS["agent"]["harnesses"]["claude"])

    cls = _ADAPTERS.get(hcfg.get("type", "template"), TemplateHarnessAdapter)
    return cls(hid, hcfg, agent_cfg)


# Wire up the built-in adapters: importing the package runs each module's
# register_adapter() call. Imported last so the names referenced above exist
# when the adapter modules import back from here.
import harnesses  # noqa: E402,F401
