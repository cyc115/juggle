#!/usr/bin/env bash
# P9 node M3-skills-scripts — migrate skills/ + scripts/ + global ~/.claude skills.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -rIn -E '\b(complete-agent|fail-agent|get-agent|send-task)\b' skills/ scripts/
