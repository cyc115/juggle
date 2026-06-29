#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && ! git grep -qnE "update_thread\([^)]*status=.background|UPDATE threads SET status" -- src/juggle_dispatch_core.py src/juggle_watchdog.py && PYTHONPATH=src uv run python -c "import dbops.threads, juggle_watchdog, juggle_dispatch_core, juggle_cmd_context, juggle_context_startup" && uv run pytest tests/test_dispatch_node.py tests/test_db_thread_state.py tests/test_p8_conv_read_collapse.py tests/test_thread_label_alloc_atomic.py -q
