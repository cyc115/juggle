#!/usr/bin/env bash
# P9 node M5-docs — migrate CLAUDE.md + README.md + docs/ command reference.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -rIn -E '\b(create-thread|complete-agent|init-db|db-flush|grep-vault|add-node|list-selfheal|vault-path)\b' CLAUDE.md README.md docs/
