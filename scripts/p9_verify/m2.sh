#!/usr/bin/env bash
# P9 node M2-commands-md — migrate commands/*.md slash-command callers.
set -euo pipefail
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
# Non-colliding legacy tokens: their canonical new forms do NOT contain the
# legacy string (create-thread→thread create, complete-agent→agent complete,
# get-agent→agent get, update-meta→thread update, request-action→action create,
# project-graph→graph, ack-action→action ack), so any match is an un-migrated
# call-site.
! grep -rIn -E '\b(create-thread|complete-agent|get-agent|update-meta|request-action|project-graph|ack-action)\b' commands/
# send-task is special: the canonical new form is `agent send-task` (the verb is
# literally "send-task"), so a naive \bsend-task\b flags every MIGRATED site too.
# Match the BARE legacy invocation only by dropping `agent send-task` lines first
# — same "keep the gate structurally passable while still catching un-migrated
# call-sites" rationale as m3.sh's --exclude-dir=p9_verify. A non-empty result is
# a bare, un-migrated `send-task`.
test -z "$(grep -rIn -E '\bsend-task\b' commands/ | grep -v 'agent send-task' || true)"
