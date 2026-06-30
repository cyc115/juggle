#!/usr/bin/env bash
# P9 node R4-switch-entrypoint — main() uses build_parser(); delete the 4 register() walls; LOC-gate green.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_loc_gate.py && uv run pytest -q
