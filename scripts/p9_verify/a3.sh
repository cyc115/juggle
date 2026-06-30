#!/usr/bin/env bash
# P9 node A3-output-parity — snapshot test: legacy name stdout == new name stdout.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_cli_alias_parity.py
