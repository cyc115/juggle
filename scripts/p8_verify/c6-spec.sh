#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && uv run pytest tests/test_spec_as_built.py -q && ! git grep -qnE "LOCKED design|^LOCKED" -- specs/2026-06-18-unified-topic-graph.md
