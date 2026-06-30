#!/usr/bin/env bash
# P9 node M3-skills-scripts — migrate skills/ + scripts/ + global ~/.claude skills.
# The §6 gate greps skills/ scripts/ for legacy tokens. We exclude scripts/p9_verify
# because the verify wrappers (this file's own grep pattern, x2.sh, m1.sh, …)
# legitimately CONTAIN legacy tokens as gate search-strings — without the exclude
# the gate would be structurally un-passable. Intent: production call-sites clean.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -rIn -E --exclude-dir=p9_verify '\b(complete-agent|fail-agent|get-agent|send-task)\b' skills/ scripts/
