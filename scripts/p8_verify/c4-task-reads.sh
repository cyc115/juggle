#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && uv run pytest tests/test_db_graph.py tests/test_graph_dispatch.py tests/test_graph_scheduler.py tests/test_node_transition.py -q
