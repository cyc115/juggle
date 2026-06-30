#!/usr/bin/env bash
# P9 node R1-spec-scaffold — add juggle_cli_spec.py (Cmd/Arg dataclasses), no behavior change.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q && uv run src/juggle_cli.py --help >/dev/null
