#!/usr/bin/env bash
# P9 node R3-port-threads — port the 4 parser walls into COMMANDS entries (legacy names canonical).
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run src/juggle_cli.py doctor --dry-run && uv run pytest -q
