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

from juggle_task_templates import TASK_TEMPLATES
from juggle_harness_defaults import HARNESS_DEFAULTS

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
    # Verify-fallback (self-heal): bounded-retry budget for a task whose real
    # verify_cmd was red. 0 disables retries (straight to terminal escalation).
    "verify_fallback_retries": 1,
    # Task Templates — prepended to agent prompts by role (extracted to
    # juggle_task_templates.py; imported back so the runtime structure is
    # unchanged — DEFAULTS["task_templates"]["coder"] etc. is byte-identical).
    "task_templates": TASK_TEMPLATES,
    # Agent runtime (2026-06-30 agent model/effort config): the model + reasoning
    # effort a dispatched agent launches with, resolved via juggle_agent_runtime.
    # Cascade (lowest→highest): built-in default → agents.model/effort (global) →
    # agents.by_role[role] → per-dispatch --model/--effort flag. `effort` is one of
    # low|medium|high|xhigh|max; None → the harness omits --effort. Empty here =
    # built-in default ("sonnet" model, no effort override).
    "agents": {
        "model": None,
        "effort": None,
        "by_role": {},
    },
    # Integrate command options
    "integrate": {
        # Directive (2026-06-20): integrate ALWAYS runs the FULL test suite,
        # never a subset. "full" is the only path — there is no scoping and no
        # --deselect quarantine. These keys are retained as inert config (empty)
        # only so existing config.json files that still set them do not crash;
        # juggle_cmd_integrate no longer reads them.
        "test_scope": "full",
        "core_tests": [],
        "quarantine_tests": [],
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
    # Watchdog daemon lifecycle (2026-06-20): global daemon cap (<=0 disables) +
    # ensure-watchdog respawn-debounce window (secs). See juggle_reaper / _lifecycle.
    "watchdog": {"max_daemons": 8, "min_respawn_interval_secs": 60},
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
        "harnesses": HARNESS_DEFAULTS,
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
        "synthesis": {
            "openrouter_model": "google/gemini-2.5-flash",
            "fallback_model": "claude-sonnet-4-6",
            "max_tokens": 2048,
        },
    },
    # DB mode — opt-in tmpfs for COW-filesystem corruption protection
    "db": {
        "mode": "direct",      # "direct" | "tmpfs"
        "tmpfs_dir": "/dev/shm",
        "flush_interval_s": 10,
    },
    # Self-heal auto-diagnosis loop (opt-in — enabled=False by default)
    "selfheal": {
        "enabled": False,
        "min_count": 3,
        "retention_days": 14,
        # selfheal-triage-v2 P1
        "allowlist_sweep_enabled": True,   # deterministic anchored sweep -> non_issue
        "resurface_surge_count": 20,       # count jump since classification that re-alerts
        "resurface_absolute_count": 100,   # cumulative count ceiling (slow-burn catch)
        "resurface_lease_days": 30,        # periodic re-confirm of still-benign groups
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


def _coerce_int(value):
    """Return int(value) or None if missing / not a clean integer."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_raw_config(env) -> dict:
    """Read ~/.juggle/config.json (``_JUGGLE_CONFIG_PATH`` override). {} on error."""
    path = Path(
        env.get("_JUGGLE_CONFIG_PATH", str(Path.home() / ".juggle" / "config.json"))
    )
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def resolve_max_agents(env=None, config=None) -> int:
    """The SINGLE source of truth for the background-agent pool cap (#5045).

    Precedence: env ``JUGGLE_MAX_BACKGROUND_AGENTS`` (explicit optional override)
    > config ``max_agents`` > ``DEFAULTS``. Config is made *authoritative* for the
    long-lived watchdog daemon at the spawn seam: the cockpit pins the daemon's
    env from CONFIG via ``config_max_agents`` (which drops the inherited override),
    so a stale inherited env can never inflate the daemon's cap above config —
    the 2026-07-01 integrate-storm root cause.

    ``env`` / ``config`` are injectable for tests; when omitted they are read from
    ``os.environ`` and ``~/.juggle/config.json`` (``_JUGGLE_CONFIG_PATH`` override).
    Invalid values fall back — never crashes.
    """
    if env is None:
        env = os.environ
    if config is None:
        config = _load_raw_config(env)

    from_env = _coerce_int(env.get("JUGGLE_MAX_BACKGROUND_AGENTS"))
    if from_env is not None:
        return from_env

    from_cfg = _coerce_int(config.get("max_agents")) if isinstance(config, dict) else None
    if from_cfg is not None:
        return from_cfg

    return DEFAULTS["max_agents"]


def config_max_agents(env=None) -> int:
    """CONFIG-authoritative pool cap: ``resolve_max_agents`` with the inherited
    ``JUGGLE_MAX_BACKGROUND_AGENTS`` override dropped (config.json path vars kept).
    This is the value the cockpit pins into the daemon's env so a stale inherited
    override can never inflate the cap above config (#5045)."""
    src = os.environ if env is None else env
    kept = {k: v for k, v in src.items() if k != "JUGGLE_MAX_BACKGROUND_AGENTS"}
    return resolve_max_agents(env=kept)


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
    if "JUGGLE_VERIFY_FALLBACK_RETRIES" in os.environ:
        settings["verify_fallback_retries"] = int(
            os.environ["JUGGLE_VERIFY_FALLBACK_RETRIES"]
        )
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
