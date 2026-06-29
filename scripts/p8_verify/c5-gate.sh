#!/usr/bin/env bash
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10 && uv run pytest tests/test_p8_readiness.py -q && PYTHONPATH=src uv run python -c "import sqlite3; from pathlib import Path; from dbops.p8_readiness import pre_p8_report; r=pre_p8_report(sqlite3.connect(':memory:'), Path('src')); assert isinstance(r['static']['excluded_files'], list); assert r['static']['import_refs']==0, r['static']['import_refs']; print('gate honest')"
