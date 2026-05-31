#!/usr/bin/env python3
"""Pluggable harness adapters for launching juggle sub-agents — framework.

Juggle spawns each background agent as a full interactive CLI process inside a
tmux pane (see ``juggle_tmux.JuggleTmuxManager``). Historically that process was
hard-wired to Claude Code. This module is the **framework**: the
``HarnessAdapter`` contract plus the registry and resolver. Each concrete
harness is **self-contained in its own module** under ``src/harnesses/`` and
owns, in one place:

  * launch          — binary, flags, env (``build_launch_command``)
  * restriction     — how per-role tool/permission limits are materialized
                      (``_restrictions_part`` — inline flags or a written file)
  * context delivery — how the role anchor reaches the agent
                      (``decorate_task`` — via hooks, or inlined into the prompt)
  * capabilities    — ``supports_hooks`` + tmux readiness/submission markers

Harnesses differ in ways a single launch string cannot capture:
  * Claude Code: JSON ``~/.claude`` settings, ``--settings <file>`` overlay,
    ``permissions.deny`` tool-name list, full hooks engine.
  * Codex CLI: TOML ``~/.codex/config.toml``, ``-c key=value`` overrides,
    sandbox/approval *modes* (not a tool list), ``AGENTS.md`` context, hooks
    only in newer versions. So the Codex adapter materializes restrictions as
    ``-a/-s`` flags and inlines the anchor by default — a different *strategy*,
    encapsulated in ``harnesses/codex.py``.

Config selection (in ``~/.juggle/config.json`` under ``agent``):
  * ``harness``            — global default harness id.
  * ``harness_by_role``    — optional per-role override.
  * ``harnesses[id]``      — harness definitions (schema in docs/harness-adapters.md).

Back-compat: a config with no ``harnesses`` block synthesises the built-in
claude harness from ``agent.claude_launch_command`` so older configs are
unchanged.
"""

from juggle_settings import get_settings

# Built-in Claude Code defaults. Doubles as the synthesised fallback when a
# config has no `harnesses` block (older configs) — keeps behaviour identical.
# The concrete logic lives in harnesses/claude.py; this dict is the data the
# framework falls back to so it must stay importable without that module.
_CLAUDE_DEFAULTS: dict = {
    "type": "claude",
    "model_flag": "--model {model}",
    "env": {"JUGGLE_IS_AGENT": "1"},
    "env_unset": ["CLAUDE_PLUGIN_DATA"],
    "readiness_markers": ["bypass permissions on", "/effort"],
    "submission_markers": ["esc to interrupt", "✻", "✶"],
    "supports_hooks": True,
}


# --------------------------------------------------------------------------
# Registry — concrete adapters self-register from their own modules.
# --------------------------------------------------------------------------
_ADAPTERS: dict = {}
_DEFAULTS_BY_TYPE: dict = {}
_LOADED = False


def register_adapter(type_name: str, cls, defaults: dict | None = None) -> None:
    """Register a concrete adapter class under a ``type`` name.

    ``defaults`` (optional) is the shipped harness-definition dict for this
    type, used by tooling/tests to synthesise a realistic config for the type.
    Called from each ``harnesses/<name>.py`` module at import time.
    """
    _ADAPTERS[type_name] = cls
    if defaults is not None:
        _DEFAULTS_BY_TYPE[type_name] = defaults


def _ensure_adapters_loaded() -> None:
    """Import the harnesses package once so all adapters self-register.

    Idempotent and lazy — keeps ``import juggle_harness`` free of a hard
    dependency on the concrete adapter modules (which import back from here).
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        import harnesses  # noqa: F401  (its __init__ imports every adapter module)
    except Exception:
        # A broken/optional adapter module must not break harness resolution;
        # the built-in template adapter is always available as a fallback.
        pass


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
        if not model:
            return ""
        return self._cfg.get("model_flag", "--model {model}").format(model=model)

    def _restrictions_part(self, role: str | None, audit: bool) -> str:
        """Flag fragment that applies per-role tool restrictions.

        Base / template behaviour: emit the static ``restrictions_flag`` from
        config (empty by default). Override in subclasses that generate files
        (Claude) or emit harness-native flags (Codex).
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

    def build_task_command(
        self,
        prompt_file: str,
        role: str | None = None,
        model: str | None = None,
        audit: bool = False,
    ) -> str:
        """Return a one-shot shell command that runs ``prompt_file`` to completion.

        Only meaningful for non-interactive harnesses. It is the launch command
        plus the prompt, supplied via ``prompt_arg`` (a format string taking
        ``{prompt_file}``, default ``"$(cat {prompt_file})"`` so the file's text
        becomes the positional prompt without shell-escaping multi-line content).
        """
        launch = self.build_launch_command(role=role, model=model, audit=audit)
        prompt_arg = self._cfg.get("prompt_arg", '"$(cat {prompt_file})"')
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
    _ensure_adapters_loaded()
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


def __getattr__(name: str):
    """Lazily re-export concrete adapters for back-compat (e.g.
    ``from juggle_harness import ClaudeCodeAdapter``) without importing the
    harnesses package at module load."""
    if name in ("ClaudeCodeAdapter", "CodexAdapter"):
        _ensure_adapters_loaded()
        module = {"ClaudeCodeAdapter": "claude", "CodexAdapter": "codex"}[name]
        import importlib

        return getattr(importlib.import_module(f"harnesses.{module}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
