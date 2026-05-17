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
from functools import lru_cache
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
    "summary_max_chars": 250,
    "thread_idle_threshold_secs": 1800,
    "thread_archive_threshold_secs": 172800,

    # Cockpit Display
    "cockpit": {
        "refresh_interval_secs": 1.0,
        "column_ratios": [0.30, 0.40, 0.30],
        "notification_ratio": 30,
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
        # Tools denied for ALL agent roles (reduces tool-definition token cost).
        # opentabs (78 browser tools) + meta tools agents never invoke.
        "disallowed_tools_universal": [
            # opentabs browser tools
            "mcp__opentabs__browser_add_tabs_to_group",
            "mcp__opentabs__browser_clear_console_logs",
            "mcp__opentabs__browser_clear_emulation",
            "mcp__opentabs__browser_clear_network_throttle",
            "mcp__opentabs__browser_clear_site_data",
            "mcp__opentabs__browser_click_element",
            "mcp__opentabs__browser_close_tab",
            "mcp__opentabs__browser_close_window",
            "mcp__opentabs__browser_create_bookmark",
            "mcp__opentabs__browser_create_tab_group",
            "mcp__opentabs__browser_create_window",
            "mcp__opentabs__browser_delete_cookies",
            "mcp__opentabs__browser_disable_network_capture",
            "mcp__opentabs__browser_download_file",
            "mcp__opentabs__browser_emulate_device",
            "mcp__opentabs__browser_emulate_vision_deficiency",
            "mcp__opentabs__browser_enable_network_capture",
            "mcp__opentabs__browser_execute_script",
            "mcp__opentabs__browser_export_har",
            "mcp__opentabs__browser_fail_request",
            "mcp__opentabs__browser_focus_tab",
            "mcp__opentabs__browser_force_pseudo_state",
            "mcp__opentabs__browser_fulfill_request",
            "mcp__opentabs__browser_get_console_logs",
            "mcp__opentabs__browser_get_cookies",
            "mcp__opentabs__browser_get_css_coverage",
            "mcp__opentabs__browser_get_download_status",
            "mcp__opentabs__browser_get_element_styles",
            "mcp__opentabs__browser_get_network_requests",
            "mcp__opentabs__browser_get_page_html",
            "mcp__opentabs__browser_get_recently_closed",
            "mcp__opentabs__browser_get_resource_content",
            "mcp__opentabs__browser_get_storage",
            "mcp__opentabs__browser_get_tab_content",
            "mcp__opentabs__browser_get_tab_info",
            "mcp__opentabs__browser_get_visits",
            "mcp__opentabs__browser_get_websocket_frames",
            "mcp__opentabs__browser_handle_dialog",
            "mcp__opentabs__browser_hover_element",
            "mcp__opentabs__browser_intercept_requests",
            "mcp__opentabs__browser_list_bookmark_tree",
            "mcp__opentabs__browser_list_downloads",
            "mcp__opentabs__browser_list_resources",
            "mcp__opentabs__browser_list_tab_groups",
            "mcp__opentabs__browser_list_tabs",
            "mcp__opentabs__browser_list_tabs_in_group",
            "mcp__opentabs__browser_list_windows",
            "mcp__opentabs__browser_navigate_tab",
            "mcp__opentabs__browser_notify",
            "mcp__opentabs__browser_open_tab",
            "mcp__opentabs__browser_press_key",
            "mcp__opentabs__browser_query_elements",
            "mcp__opentabs__browser_remove_tabs_from_group",
            "mcp__opentabs__browser_restore_session",
            "mcp__opentabs__browser_screenshot_tab",
            "mcp__opentabs__browser_scroll",
            "mcp__opentabs__browser_search_bookmarks",
            "mcp__opentabs__browser_search_history",
            "mcp__opentabs__browser_select_option",
            "mcp__opentabs__browser_set_cookie",
            "mcp__opentabs__browser_set_geolocation",
            "mcp__opentabs__browser_set_media_features",
            "mcp__opentabs__browser_stop_intercepting",
            "mcp__opentabs__browser_throttle_network",
            "mcp__opentabs__browser_type_text",
            "mcp__opentabs__browser_update_tab_group",
            "mcp__opentabs__browser_update_window",
            "mcp__opentabs__browser_wait_for_element",
            "mcp__opentabs__extension_check_adapter",
            "mcp__opentabs__extension_force_reconnect",
            "mcp__opentabs__extension_get_logs",
            "mcp__opentabs__extension_get_side_panel",
            "mcp__opentabs__extension_get_state",
            "mcp__opentabs__extension_reload",
            "mcp__opentabs__plugin_analyze_site",
            "mcp__opentabs__plugin_inspect",
            "mcp__opentabs__plugin_list_tabs",
            "mcp__opentabs__plugin_mark_reviewed",
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
        ],
        # Per-role role identity sentences injected into agent context anchor.
        "role_context": {
            "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
            "coder":      "Implement exactly what is specified — no more. Minimal diff.",
            "planner":    "Produce plans a coder can execute without clarification.",
        },
        # Skill invoked by coder agents before complete-agent (configurable per deployment).
        "quality_gate_skill": "mike:pre-pr",
        # Per-role additional denylists (merged with universal at spawn time).
        "disallowed_tools_by_role": {
            "researcher": [
                "Edit",          # researchers don't patch code
                "NotebookEdit",  # no Jupyter in Juggle
            ],
            "coder": [
                "NotebookEdit",                              # no Jupyter in Juggle
                "mcp__personal-mcp__extract_text_from_file", # OCR not needed for coding
            ],
            "planner": [
                "Edit",                                      # planners write plans, not code
                "NotebookEdit",                              # no Jupyter in Juggle
                "Monitor",                                   # planners don't run bg processes
                "TaskOutput",                                # no bg tasks to monitor
                "TaskStop",                                  # no bg tasks to stop
                "mcp__personal-mcp__extract_text_from_file", # OCR not needed for planning
            ],
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


@lru_cache(maxsize=1)
def get_settings() -> dict:
    """Return merged settings dict. Cached for process lifetime.

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
        settings["tmux"]["agent_idle_detection_secs"] = int(os.environ["JUGGLE_IDLE_THRESHOLD_SECS"])
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
