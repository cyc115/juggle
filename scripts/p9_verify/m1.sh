#!/usr/bin/env bash
# P9 node M1-templates — migrate agent-dispatch templates (juggle_context.py + juggle_task_templates.py).
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -nE '\b(complete-agent|fail-agent)\b' src/juggle_context.py src/juggle_task_templates.py
