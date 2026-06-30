#!/usr/bin/env bash
# P9 node D1-warn-on — flip rewrite_argv warn=True (stderr only); stdout unchanged.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_cli_deprecation_warning.py
