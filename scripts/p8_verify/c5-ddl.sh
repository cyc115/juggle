#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && uv run pytest tests/test_nodes_schema_migration.py -q && PYTHONPATH=src uv run python -c "import sqlite3; from dbops.schema_nodes import CREATE_NODES; c=sqlite3.connect(':memory:'); c.execute(CREATE_NODES); cols={r[1] for r in c.execute('PRAGMA table_info(nodes)')}; need={'user_label','assigned_by','last_active_at','dispatch_thread_id'}; assert need<=cols, need-cols; print('DDL complete')"
