#!/usr/bin/env bash
# P9 node G3-verb-lint — closed-verb-vocabulary lint test (every COMMANDS.verb in allowlist).
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_cli_verb_vocab.py
