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
    "notification_max_delivery_attempts": 3,
    "summary_max_chars": 250,

    # Cockpit Display
    "cockpit": {
        "refresh_interval_secs": 1.0,
        "column_ratios": [0.30, 0.40, 0.30],
        "max_nudge_lines": 3,
        "max_notification_rows": 4,
        "idle_open_question_threshold_secs": 7200,
        "stale_blocker_threshold_secs": 14400,
        "thread_idle_threshold_secs": 1800,
        "thread_archive_threshold_secs": 172800,
    },

    # Paths
    "paths": {
        "data_dir": "~/.claude/juggle",
        "config_dir": "~/.juggle",
        "digest_log_dir": "~/.juggle/logs",
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
        "recall_join_timeout_secs": 10,
        "reflect_timeout_secs": 60,
    },

    # Domain Seeds
    "domains": {
        "initial_domains": ["juggle", "vault", "work"],
        "initial_domain_paths": [
            ["/github/juggle", "juggle"],
            ["/Documents/personal", "vault"],
            ["/work/", "work"],
        ],
    },

    # Agent Launch
    "agent": {
        "claude_launch_command": "claude --dangerously-skip-permissions",
    },

    # Talkback TTS
    "talkback": {
        "enabled": False,
        "port": 18787,
        "voice": "af_heart",
        "speed": 1.0,
        "max_speak_chars": 200,
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
