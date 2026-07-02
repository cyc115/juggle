"""juggle_harness_defaults — the built-in sub-agent harness adapter defaults.

Extracted from juggle_settings.DEFAULTS["agent"]["harnesses"] (2026-06-30 agent
model/effort config) so the settings module stays within its LOC budget. Pure
data; imported back as ``HARNESS_DEFAULTS`` so the runtime structure is
byte-identical. See juggle_harness.py and docs/harness-adapters.md for the schema.
"""
from __future__ import annotations

HARNESS_DEFAULTS: dict = {
    "claude": {
        # Built-in adapter: per-role tool denies via a `--settings`
        # overlay (juggle_agent_settings). `command` is omitted so it
        # falls back to `agent.claude_launch_command` above — one source
        # of truth for the Claude launch string.
        "type": "claude",
        "model_flag": "--model {model}",
        # Claude Code's launch flag for reasoning effort (2026-06-30 agent
        # model/effort config). Omitted when no effort is resolved.
        "effort_flag": "--effort {effort}",
        # Harness-specific env overrides (override inherited values). The
        # JUGGLE_IS_AGENT/ROLE/AUDIT identity vars are injected
        # automatically — add your own here.
        "env": {},
        "env_unset": ["CLAUDE_PLUGIN_DATA"],
        # "shift+tab to cycle" is the stable structural marker of a ready input
        # box — present in every permission mode and model, and it persists when
        # idle (defect E, 2026-07-01: juggle spawns in accept-edits mode, so
        # "bypass permissions on" never matched and "/effort" is transient).
        "readiness_markers": ["shift+tab to cycle", "bypass permissions on", "/effort"],
        "submission_markers": ["esc to interrupt", "✻", "✶"],
        # Structural fallback for "is actively processing" that doesn't depend
        # on enumerating spinner glyphs/verbs (2026-07-02 false-positive stall
        # nudge on agent ZJ: glyph '✢' wasn't in submission_markers, so the
        # always-present footer marker fired while the agent was mid-task).
        # Every active-processing status line shows an elapsed-time + '↓
        # <count>k? tokens' suffix regardless of which glyph/verb is showing —
        # e.g. "✢ Waddling… (24m 30s · ↓ 29.7k tokens)".
        "active_status_pattern": r"\(\d+[hms][^()]*↓[^()]*tokens\)",
        "supports_hooks": True,
    },
    # Built-in Codex CLI adapter (src/harnesses/codex.py). Inactive
    # until selected via `harness` / `harness_by_role`. Codex restricts
    # via sandbox/approval MODES (not a tool-deny list), reads AGENTS.md
    # for context, and has version-skewed hooks — so per-role limits are
    # materialized as `-a/-s` flags and the role anchor is inlined
    # (supports_hooks=False). Confirm the `command`/markers against your
    # installed `codex` and override here if needed; no code change.
    "codex": {
        "type": "codex",
        "command": "codex exec",
        "interactive": False,
        "model_flag": "-m {model}",
        # Pin the Codex model (gpt-*, not sonnet/opus) regardless of the
        # agent's configured model; empty = use the per-agent model.
        "model": "",
        # Arbitrary extra CLI flags appended verbatim (e.g. "-c key=val").
        "extra_flags": "",
        # `codex exec - < prompt.txt` — `-` reads the prompt from stdin
        # (avoids ARG_MAX + a non-TTY-pipe hang); see harnesses/codex.py.
        "prompt_arg": "- < {prompt_file}",
        "approval_policy": "never",
        "sandbox_by_role": {
            "researcher": "read-only",
            "planner": "read-only",
            "coder": "workspace-write",
        },
        "sandbox_default": "read-only",
        "sandbox_audit": "workspace-write",
        "restrictions_flag": "",
        "env": {},
        "env_unset": [],
        # No readiness/submission markers: one-shot harnesses don't poll
        # a REPL (see is_interactive=False above).
        "supports_hooks": False,
    },
    # Reasonix (deepseek-reasonix) CLI. Inactive until selected via
    # `harness` / `harness_by_role`. A config-only `template` harness —
    # no Python needed: one-shot `reasonix run` reading the prompt from
    # stdin, model via `--model`, AGENTS.md context, anchor inlined.
    # Tool restriction is delegated to the harness's own reasonix.toml
    # ([permissions]/[sandbox], workspace confinement) — Reasonix exposes
    # no per-call restriction flags — so `external_restriction` is set.
    #
    # Configured for OpenRouter's DeepSeek-V4 Pro: `model` is the Reasonix
    # provider NAME (passed as `--model`); define that provider in
    # reasonix.toml pointing base_url at OpenRouter and model at
    # `deepseek/deepseek-v4-pro` (see docs/reasonix.toml.example). Export
    # OPENROUTER_KEY in juggle's environment so launched agents
    # inherit it.
    "reasonix": {
        "type": "template",
        "command": "reasonix run",
        "interactive": False,
        "model_flag": "--model {model}",
        # Reasonix provider name (defined in reasonix.toml) → OpenRouter
        # DeepSeek-V4 Pro. Overridable.
        "model": "deepseek-v4-pro",
        "extra_flags": "",
        # `reasonix run` reads the prompt from stdin.
        "prompt_arg": "< {prompt_file}",
        "restrictions_flag": "",
        "external_restriction": True,
        "env": {},
        "env_unset": [],
        "supports_hooks": False,
    },
}
