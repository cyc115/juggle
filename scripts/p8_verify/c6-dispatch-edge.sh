#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && uv run pytest tests/test_dispatch_node.py tests/test_nodes_schema_migration.py -q && [ "$(git grep -lnE 'dispatch_thread_id' -- src/ | grep -vE 'migration_|schema_nodes' | wc -l | tr -d ' ')" = "0" ]
