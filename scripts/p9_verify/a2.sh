#!/usr/bin/env bash
# P9 node A2-alias-coverage — assert ALIASES covers all 61 legacy names; add 'aliases --json'.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run src/juggle_cli.py aliases --json | uv run python -c 'import sys,json; d=json.load(sys.stdin); req=open("tests/data/legacy_names.txt").read().split(); sys.exit(0 if set(req)<=set(d) else 1)'
