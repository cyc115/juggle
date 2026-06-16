#!/usr/bin/env python3
"""Juggle Settings — unified configuration loader.

Load order (highest → lowest precedence):
  1. Env var overrides (JUGGLE_MAX_THREADS, etc.)
  2. ~/.juggle/config.json  (or _JUGGLE_CONFIG_PATH env override)
  3. DEFAULTS (hardcoded in this file)

Usage:
    from juggle_settings import get_settings, get, get_nested
    s = get_settings()
    s["max_threads"]                        # top-level key
    s["cockpit"]["refresh_interval_secs"]   # nested key
    get("max_threads")                      # helper shortcut
    get_nested("cockpit", "refresh_interval_secs")
"""

import json
import os
from pathlib import Path

DEFAULTS: dict = {
    # Limits & Thresholds
    "max_threads": 10,
    "max_agents": 20,
    "agent_idle_ttl_secs": 43200,
    "message_history_token_budget": 1500,
    "context_injection_char_limit": 8000,
    "context_teaser_chars": 80,
    "stale_summary_message_threshold": 3,
    "agent_boot_grace_secs": 120,
    "summary_max_chars": 250,
    # Per-repo integration config. Key = absolute repo path.
    # Example: {"/home/user/juggle": {"push_mode": "direct", "test_cmd": "pytest"}}
    # push_mode: "direct" = ff-merge+push main | "pr" = push branch only | "none" = local merge only
    "repos": {},
    "thread_idle_threshold_secs": 1800,
    "thread_archive_threshold_secs": 172800,
    # Task Templates — prepended to agent prompts by role
    "task_templates": {
        "coder": (
            "## Role: Coder\n\n"
            "Implement exactly what is specified — no more. Minimal diff.\n\n"
            "### TDD Discipline\n"
            "1. Write failing tests FIRST — confirm they FAIL before implementation\n"
            "2. Implement the minimum code to pass tests\n"
            "3. Run the full test suite — fix any regressions\n"
            "4. Run pre-pr quality gate ({quality_gate_skill}) before completion\n\n"
            "### Completion Protocol\n"
            "When finished, call: juggle complete-agent <thread> \"<summary>\" --retain \"<key finding>\"\n"
            "Pre-existing test failures are NOT your concern — document in --retain and proceed.\n\n"
            "### Scope\n"
            "- Only files directly related to the task\n"
            "- No refactoring, cleanup, or bonus work\n"
            "- Do NOT modify AGENTS.md, CLAUDE.md, or .codegraph files\n\n"
            "HARNESS GATE: run the repo's harness smoke suite "
            "(trading-edge: `uv run pytest -m pilot`; "
            "juggle: full pytest + doctor --dry-run on a tmp DB) "
            "and paste the suite summary line in your completion result. "
            "Completion without harness evidence is invalid.\n"
        ),
        "planner": (
            "## Role: Planner\n\n"
            "Produce plans a coder can execute without clarification.\n\n"
            "### Plan Requirements\n"
            "- Every step must be verifiable by an agent (deterministic command + expected output)\n"
            "- Batch unresolved questions in --open-questions; do not ask interactively\n"
            "- Include devil's-advocate section: weakest assumption per fix + failure mode + mitigation\n\n"
            "### Completion Protocol\n"
            "When finished, call: juggle complete-agent <thread> \"<summary>\" --open-questions '<json>'\n\n"
            "### Scope\n"
            "- Write the plan file only — never implement\n"
            "- No research beyond what's needed to ground the plan in real code\n"
            "- Open the plan in Obsidian after writing\n"
        ),
        "researcher": (
            "## Role: Researcher\n\n"
            "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.\n\n"
            "### Research Standards\n"
            "- Cite sources with URLs and retrieval dates\n"
            "- Distinguish facts from opinions\n"
            "- Cross-reference at least 2 sources for key claims\n\n"
            "### Completion Protocol\n"
            "When finished, call: juggle complete-agent <thread> \"<summary>\" --retain \"<key finding>\"\n\n"
            "### Scope\n"
            "- Research only — no implementation, no code changes\n"
            "- Stay within the research topic; no tangent deep-dives\n"
        ),
    },
    # Integrate command options
    "integrate": {
        # "changed" = scope tests to branch-changed files (default).
        # "full"    = always run the full test_cmd (old behaviour).
        "test_scope": "changed",
        # fnmatch globs (relative to repo root) always added to scoped runs.
        "core_tests": [],
        # Pre-existing RED tests excluded from both scoped and full runs via
        # --deselect.  Shrinks as each is fixed: loc_gate awaits dbops refactor
        # + budget-lower; data_migration awaits triage.  NOT permanent.
        "quarantine_tests": [
            "tests/test_loc_gate.py",
            "tests/test_data_migration.py",
        ],
    },
    # Cockpit Display
    "cockpit": {
        "refresh_interval_secs": 1.0,
        "column_ratios": [0.30, 0.40, 0.30],
        "notification_ratio": 30,
        "bell": True,
        "desktop_notifications": False,
    },
    # Paths
    "paths": {
        "data_dir": "~/.claude/juggle",
        "config_dir": "~/.juggle",
        "digest_log_dir": "~/.juggle/logs",
        "vault": "/Documents/personal",
        "vault_name": "",
    },
    # Tmux
    "tmux": {
        "session_name": "juggle",
        "session_width": 220,
        "session_height": 50,
        "agent_idle_detection_secs": 30,
        # send-task readiness backoff: poll the pane for a Claude-UI readiness
        # marker up to N times, sleeping `ready_poll_interval_secs` between
        # attempts, before giving up. Total wait ≈ attempts × interval.
        # Default: 1s poll for ~120s — fast detection, generous cold-start budget.
        "ready_poll_attempts": 120,
        "ready_poll_interval_secs": 1,
    },
    # Hindsight
    "hindsight": {
        "enabled": False,
        "api_url": "http://localhost:18888",
        "api_key": "juggle",
        "bank": "juggle",
        "timeout_secs": 10,
        "reflect_timeout_secs": 60,
    },
    # Agent Launch
    "agent": {
        "claude_launch_command": "claude --dangerously-skip-permissions",
        # Per-role role identity sentences injected into agent context anchor.
        "role_context": {
            "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
            "coder": "Implement exactly what is specified — no more. Minimal diff.",
            "planner": "Produce plans a coder can execute without clarification.",
        },
        # Skill invoked by coder agents before complete-agent (configurable per deployment).
        "quality_gate_skill": "mike:pre-pr",
        # Audit (measurement) mode for right-sizing the deny block. When true,
        # build_agent_overlay drops the PER-ROLE denials so those tools stay in
        # the agent's context and `juggle agent-tools` can observe real per-role
        # demand (stripped tools are invisible — they're never offered to the
        # model, so they leave no usage signal). Agents launched in this mode set
        # JUGGLE_AGENT_AUDIT=1 so their telemetry is tagged 'audit'. Universal
        # (settings_overlay_base) denials stay in effect — flip an entry out of
        # base temporarily if you need to audit those too. Costs tokens while on
        # (tools re-enter context); turn off to bank the savings again.
        "audit_mode": False,
        # --- Sub-agent harness adapters --------------------------------------
        # Which CLI juggle launches for each background agent. Default "claude"
        # (Claude Code). Point a deployment at a different harness (Codex, or any
        # future CLI) WITHOUT code changes: add an entry under `harnesses` and
        # set `harness` (global) or `harness_by_role` (per role). See
        # juggle_harness.py and docs/harness-adapters.md for the full schema and
        # a worked Codex/reasonix example.
        "harness": "claude",
        # Optional per-role override, e.g. {"researcher": "codex"}.
        "harness_by_role": {},
        "harnesses": {
            "claude": {
                # Built-in adapter: per-role tool denies via a `--settings`
                # overlay (juggle_agent_settings). `command` is omitted so it
                # falls back to `agent.claude_launch_command` above — one source
                # of truth for the Claude launch string.
                "type": "claude",
                "model_flag": "--model {model}",
                # Harness-specific env overrides (override inherited values). The
                # JUGGLE_IS_AGENT/ROLE/AUDIT identity vars are injected
                # automatically — add your own here.
                "env": {},
                "env_unset": ["CLAUDE_PLUGIN_DATA"],
                "readiness_markers": ["bypass permissions on", "/effort"],
                "submission_markers": ["esc to interrupt", "✻", "✶"],
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
            # OPENROUTER_API_KEY in juggle's environment so launched agents
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
        },
        # --- Agent settings.json overlay -------------------------------------
        # Each agent's settings.json is generated from these two keys
        # (juggle_agent_settings.build_agent_overlay) and passed via
        # `--settings <file>`. `--settings` LAYERS over the host settings
        # hierarchy — omitted keys keep their host values, and permission
        # allow/deny/ask arrays UNION across sources — so this overlay is
        # purely ADDITIVE and portable: it never replaces the host's settings.
        #
        # Composition: settings_overlay_base (universal, applied to EVERY
        # agent) is merged first, then settings_overlay_by_role[role] is merged
        # on top. List values (e.g. permissions.deny) union; nested dicts
        # deep-merge; scalars (model, defaultMode, …) override.
        #
        # permissions.deny here doubles as the token-saving lever: a bare tool
        # name removes that tool from the agent's context entirely.
        "settings_overlay_base": {
            # Force non-vim editor mode for all background agents regardless of
            # the host's global ~/.claude/settings.json (which may set vim mode).
            # Vim mode breaks tmux paste dispatch: send_task pastes into NORMAL
            # mode and the keystrokes are interpreted as editor commands.
            "editorMode": "normal",
            "permissions": {
                "deny": [
                    # opentabs browser tools (78 tools) — wildcard collapses to one entry
                    "mcp__opentabs__*",
                    # GitHub MCP (60+ tools) — the orchestrator owns all GitHub/PR
                    # work; agents do code via the git CLI (Bash). Largest single
                    # context saving. (Standard `github` MCP namespace.)
                    "mcp__github__*",
                    # otterai (meeting transcription) — not used by any agent role.
                    "mcp__otterai__*",
                    # NOTE: the claude.ai Google Workspace connectors (Drive,
                    # Calendar, Gmail) are NOT denied universally — researchers
                    # need them. They are denied per-role for coder + planner in
                    # settings_overlay_by_role below.
                    # personal-mcp financial tools (not for agents)
                    "mcp__personal-mcp__plaid_get_accounts",
                    "mcp__personal-mcp__plaid_get_statements",
                    "mcp__personal-mcp__plaid_sync_transactions",
                    # meta / orchestrator tools agents don't invoke
                    "ScheduleWakeup",
                    "CronCreate",
                    "CronList",
                    "CronDelete",
                    "ShareOnboardingGuide",
                    "ExitPlanMode",
                    "EnterPlanMode",
                    "EnterWorktree",
                    "ExitWorktree",
                    "PushNotification",
                    # sub-agent spawning and remote triggers — orchestrator-only
                    "Agent",
                    "RemoteTrigger",
                    # MCP resource browsing — not used by any agent role
                    "ListMcpResourcesTool",
                    "ReadMcpResourceTool",
                ]
            }
        },
        # Per-role overlay merged ON TOP of settings_overlay_base. Today only
        # adds role-specific denials; a role may also diverge on env / model /
        # hooks / sandbox here with no code change.
        "settings_overlay_by_role": {
            "researcher": {
                "permissions": {
                    "deny": [
                        "Edit",  # researchers don't patch code
                        "NotebookEdit",  # no Jupyter in Juggle
                    ]
                }
            },
            "coder": {
                "permissions": {
                    "deny": [
                        "NotebookEdit",  # no Jupyter in Juggle
                        "mcp__personal-mcp__extract_text_from_file",  # OCR not needed for coding
                        # claude.ai Google Workspace connectors — researchers only.
                        # VERIFY these slugs on the host via `/permissions` (add a
                        # deny rule, type `mcp__` to autocomplete): the server names
                        # contain spaces/dots and Claude Code's slug sanitization
                        # for those is undocumented. A wrong slug fails silently.
                        "mcp__claude.ai Google Drive__*",
                        "mcp__claude.ai Google Calendar__*",
                        "mcp__claude.ai Gmail__*",
                    ]
                }
            },
            "planner": {
                "permissions": {
                    "deny": [
                        "Edit",  # planners write plans, not code
                        "NotebookEdit",  # no Jupyter in Juggle
                        "Monitor",  # planners don't run bg processes
                        "TaskOutput",  # no bg tasks to monitor
                        "TaskStop",  # no bg tasks to stop
                        "mcp__personal-mcp__extract_text_from_file",  # OCR not needed for planning
                        # claude.ai Google Workspace connectors — researchers only.
                        # (Verify slugs via `/permissions`; see coder note above.)
                        "mcp__claude.ai Google Drive__*",
                        "mcp__claude.ai Google Calendar__*",
                        "mcp__claude.ai Gmail__*",
                    ]
                }
            },
        },
    },
    # Talkback TTS
    "talkback": {
        "enabled": False,
        "port": 18787,
    },
    # Research Knowledge Base
    "research_kb": {
        "db_path": "~/.juggle/research_kb.db",
        "embedding_model": "openai/text-embedding-3-small",
        "summarization_model": "~google/gemini-pro-latest",
        "hn_score_threshold": 100,
        "web_search_enabled": True,
        "pdf_dirs": [],
    },
    # Title Generation (API key lives in ~/.juggle/.env as OPENROUTER_KEY, not here)
    "title_gen": {
        "openrouter_enabled": True,
        "openrouter_model": "google/gemini-2.5-flash-lite",
        "haiku_model": "claude-haiku-4-5-20251001",
        "timeout_secs": 10,
    },
    # LLM dispatcher profiles (model ids are editable in config.json)
    "llm_profiles": {
        "cheap": {
            "openrouter_model": "deepseek/deepseek-chat-v3-0324:free",
            "fallback_model": "claude-haiku-4-5-20251001",
        },
        "normal": {
            "openrouter_model": "moonshotai/kimi-k2:free",
            "fallback_model": "claude-sonnet-4-6",
        },
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Returns a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def get_settings() -> dict:
    """Return merged settings dict. Re-reads config on every call (no cache).

    Load order: DEFAULTS → config.json → env var overrides.
    Safe to call at import time — falls back to DEFAULTS if config is missing or corrupt.
    """
    config_path = Path(
        os.environ.get(
            "_JUGGLE_CONFIG_PATH",
            str(Path.home() / ".juggle" / "config.json"),
        )
    )
    user_config: dict = {}
    if config_path.exists():
        try:
            user_config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass  # corrupt or unreadable — fall back to DEFAULTS

    settings = _deep_merge(DEFAULTS, user_config)

    # Env var overrides (backward compat — these take precedence over config.json)
    if "JUGGLE_MAX_THREADS" in os.environ:
        settings["max_threads"] = int(os.environ["JUGGLE_MAX_THREADS"])
    if "JUGGLE_MAX_BACKGROUND_AGENTS" in os.environ:
        settings["max_agents"] = int(os.environ["JUGGLE_MAX_BACKGROUND_AGENTS"])
    if "JUGGLE_IDLE_THRESHOLD_SECS" in os.environ:
        settings["tmux"]["agent_idle_detection_secs"] = int(
            os.environ["JUGGLE_IDLE_THRESHOLD_SECS"]
        )
    if "JUGGLE_READY_POLL_ATTEMPTS" in os.environ:
        settings["tmux"]["ready_poll_attempts"] = int(
            os.environ["JUGGLE_READY_POLL_ATTEMPTS"]
        )
    if "JUGGLE_READY_POLL_INTERVAL_SECS" in os.environ:
        settings["tmux"]["ready_poll_interval_secs"] = float(
            os.environ["JUGGLE_READY_POLL_INTERVAL_SECS"]
        )
    # Expand ~ in all path values
    for key in ("data_dir", "config_dir", "digest_log_dir"):
        settings["paths"][key] = str(Path(settings["paths"][key]).expanduser())

    return settings


def get(key: str, default=None):
    """Shortcut: get a top-level setting value."""
    return get_settings().get(key, default)


def get_nested(section: str, key: str, default=None):
    """Shortcut: get a nested setting value from a named section."""
    return get_settings().get(section, {}).get(key, default)


def get_repo_config(repo_path: str) -> dict:
    """Return integration config for repo_path with safe defaults.

    Unknown repos get push_mode='none' and test_cmd='' — intentionally safe.
    """
    repos = get_settings().get("repos", {})
    cfg = repos.get(str(repo_path), {})
    return {
        "push_mode": cfg.get("push_mode", "none"),
        "test_cmd": cfg.get("test_cmd", ""),
    }
