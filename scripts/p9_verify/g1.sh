#!/usr/bin/env bash
# P9 node G1-resource-groups — introduce resource subparser groups; set new canonical names.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
for c in 'thread create x' 'agent list' 'action list' 'selfheal list' 'db flush --status' 'vault path' 'schedule dogfood --dry-run'; do
  uv run src/juggle_cli.py $c --help >/dev/null || exit 1
done
