#!/usr/bin/env bash
# P9 node G2-fold-project-graph — fold 'project-graph load' into 'graph load'.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run src/juggle_cli.py graph load --help >/dev/null
