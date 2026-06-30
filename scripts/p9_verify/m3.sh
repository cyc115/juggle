#!/usr/bin/env bash
# P9 node M3-skills-scripts — migrate skills/ + scripts/ + global ~/.claude skills.
# The §6 gate greps skills/ scripts/ for legacy tokens. We exclude scripts/p9_verify
# because the verify wrappers (this file's own grep pattern, x2.sh, m1.sh, …)
# legitimately CONTAIN legacy tokens as gate search-strings — without the exclude
# the gate would be structurally un-passable. Intent: production call-sites clean.
#
# `send-task` is special: spec §2.3 KEEPS it as a compound verb, so the canonical
# migrated call-site is `agent send-task` — which still contains the `send-task`
# token. A bare \bsend-task\b would false-positive on that legitimate new form, so
# we match send-task occurrences and drop the `agent send-task` ones before
# failing. Intent: ban legacy FLAT send-task; allow the new `agent send-task`.
# (No grep -P / lookbehind — BSD grep on macOS lacks it; match-then-filter is portable.)
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
! grep -rIn -E --exclude-dir=p9_verify '\b(complete-agent|fail-agent|get-agent)\b' skills/ scripts/
legacy_send_task="$(grep -rIn -E --exclude-dir=p9_verify '\bsend-task\b' skills/ scripts/ | grep -v 'agent send-task' || true)"
[ -z "$legacy_send_task" ]
