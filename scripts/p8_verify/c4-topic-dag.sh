#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && ! git grep -ql db_mirror -- src/ && [ ! -f src/dbops/db_mirror.py ] && PYTHONPATH=src uv run python -c "import dbops.threads, dbops.db_topics, dbops.db_topics_reconcile, juggle_cmd_threads, juggle_cmd_projects, juggle_cmd_doctor" && uv run pytest tests/test_cockpit_graph_dag_load.py tests/test_graph_reconcile.py -q
