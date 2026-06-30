#!/usr/bin/env bash
# P9 node M4-tests — migrate tests/ to drive new verbs (regression pins re-targeted, not weakened).
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q
