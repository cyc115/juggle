#!/usr/bin/env bash
# P9 node R2-generic-registrar — build_parser() generic registrar driven by COMMANDS.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_cli_spec_registrar.py
