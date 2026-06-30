#!/usr/bin/env bash
# P9 node A1-alias-shim — juggle_cli_aliases.rewrite_argv + ALIASES derived from COMMANDS (silent).
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_cli_aliases.py
