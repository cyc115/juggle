#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && ! git grep -qnE "FROM threads|\['status'\]|\['topic'\]|\['last_active'\]" -- src/juggle_cmd_projects.py src/juggle_cmd_context.py src/juggle_context_startup.py src/juggle_project_summary.py src/juggle_cmd_runs.py src/juggle_cmd_agents_lifecycle.py && uv run pytest tests/test_cmd_context.py tests/test_cockpit_model.py tests/test_p8_conv_read_collapse.py -q
