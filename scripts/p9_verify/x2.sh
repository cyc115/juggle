#!/usr/bin/env bash
# P9 node X2-remove-aliases — delete legacy ALIASES entries; legacy names now exit 2; bump version.
# NOTE: X2 is gated behind manual user approval and is intentionally OMITTED from the
# auto-loader (/tmp/p9_load_dag.sh). The wrapper exists so the node can be armed by hand.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q && ! uv run src/juggle_cli.py complete-agent X 'y' 2>/dev/null && uv run src/juggle_cli.py cockpit --smoke --all-viewports --json >/dev/null
