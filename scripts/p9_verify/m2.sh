#!/usr/bin/env bash
# P9 node M2-commands-md — migrate commands/*.md slash-command callers.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -rIn -E '\b(create-thread|complete-agent|send-task|get-agent|update-meta|request-action|create-thread|project-graph|ack-action)\b' commands/
