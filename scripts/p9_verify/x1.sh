#!/usr/bin/env bash
# P9 node X1-regen-units — regenerate persisted launchd/systemd units to 'db flush'.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run src/juggle_cli.py db flush --install-supervisor && ! grep -rIn 'db-flush' "$HOME/Library/LaunchAgents" "$HOME/.config/systemd" 2>/dev/null
