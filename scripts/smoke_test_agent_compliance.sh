#!/usr/bin/env bash
# smoke_test_agent_compliance.sh
#
# Dispatch one task per role, wait up to 10 min for completion, then
# evaluate compliance flags and print PASS/FAIL per role.
#
# Usage:
#   bash scripts/smoke_test_agent_compliance.sh
#
# Requires:
#   - Juggle running (juggle start or active tmux session)
#   - python3, sqlite3, juggle_cli.py available
#   - CLAUDE_PLUGIN_ROOT set (or auto-derived from this script's location)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-${REPO_ROOT}}"
CLI="${PLUGIN_ROOT}/src/juggle_cli.py"
POLL_INTERVAL=20
MAX_WAIT=600

DB_PATH=$(python3 -c "
import sys; sys.path.insert(0, '${PLUGIN_ROOT}/src')
from juggle_settings import get_settings
from pathlib import Path
print(Path(get_settings()['paths']['data_dir']) / 'juggle.db')
")

SMOKE_TS=$(date +%s)
SMOKE_DATE=$(date +%Y-%m-%d)

log() { echo "[smoke] $*" >&2; }

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

last_assistant_message() {
    sqlite3 "${DB_PATH}" \
        "SELECT content FROM messages WHERE thread_id='${1}' AND role='assistant' ORDER BY id DESC LIMIT 1;" \
        2>/dev/null || true
}

thread_is_done() {
    local status
    status=$(sqlite3 "${DB_PATH}" "SELECT status FROM threads WHERE id='${1}';" 2>/dev/null || echo "")
    [ "$status" = "closed" ] || [ "$status" = "archived" ]
}

thread_uuid_for_label() {
    sqlite3 "${DB_PATH}" "SELECT id FROM threads WHERE user_label='${1}';" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

dispatch_task() {
    local role="$1" label="$2" task_body="$3"

    local create_out thread_label agent_info agent_id thread_uuid tmp_task
    create_out=$(python3 "${CLI}" thread create "smoke-${label}" 2>/dev/null)
    thread_label=$(echo "${create_out}" | grep -oP '(?<=Created Topic )\w+' || true)
    if [ -z "${thread_label}" ]; then
        log "ERROR: failed to create thread for ${role}"
        return 1
    fi

    agent_info=$(python3 "${CLI}" agent get "${thread_label}" --role "${role}" 2>/dev/null)
    agent_id=$(echo "${agent_info}" | awk '{print $1}')
    if [ -z "${agent_id}" ]; then
        log "ERROR: no agent available for ${role}"
        python3 "${CLI}" thread close "${thread_label}" 2>/dev/null || true
        return 1
    fi

    thread_uuid=$(thread_uuid_for_label "${thread_label}")

    tmp_task=$(mktemp /tmp/juggle_smoke_XXXXXX.txt)
    printf '%s' "${task_body}" > "${tmp_task}"
    python3 "${CLI}" agent send-task "${agent_id}" "${tmp_task}" 2>/dev/null
    rm -f "${tmp_task}"

    echo "${thread_uuid}:${thread_label}"
}

# ---------------------------------------------------------------------------
# Wait for completion
# ---------------------------------------------------------------------------

wait_for_completion() {
    local uuid="$1" role="$2" elapsed=0
    log "Waiting for ${role} (max ${MAX_WAIT}s)..."
    while [ "$elapsed" -lt "$MAX_WAIT" ]; do
        if thread_is_done "${uuid}"; then
            log "${role} completed after ${elapsed}s"
            return 0
        fi
        sleep "${POLL_INTERVAL}"
        elapsed=$((elapsed + POLL_INTERVAL))
        log "  ${role}: ${elapsed}s elapsed..."
    done
    log "TIMEOUT: ${role} did not complete within ${MAX_WAIT}s"
    return 1
}

# ---------------------------------------------------------------------------
# Compliance checks (one per role)
# ---------------------------------------------------------------------------

check_researcher() {
    local uuid="$1"
    local last_msg
    last_msg=$(last_assistant_message "${uuid}")

    local report_path
    report_path=$(echo "${last_msg}" | grep -oP '(?i)(?<=Research complete:\s)[^\s,]+\.md' || true)
    if [ -z "${report_path}" ]; then
        log "RESEARCHER: no report path in completion message"
        echo "FAIL"
        return
    fi
    case "${report_path}" in
        /*) ;;
        *)  report_path="${HOME}/Documents/personal/${report_path}" ;;
    esac
    if [ ! -f "${report_path}" ]; then
        log "RESEARCHER: report file not found: ${report_path}"
        echo "FAIL"
        return
    fi

    local markers gaps
    markers=$(grep -cP '\[HIGH CONFIDENCE\]|\[CONFLICTING\]|\[UNVERIFIED\]' "${report_path}" 2>/dev/null || echo 0)
    gaps=$(grep -cP '^## Gaps' "${report_path}" 2>/dev/null || echo 0)
    log "RESEARCHER: confidence_markers=${markers} gaps_section=${gaps}"
    if [ "$markers" -ge 1 ] && [ "$gaps" -ge 1 ]; then
        echo "PASS"
    else
        echo "FAIL markers=${markers} gaps=${gaps}"
    fi
}

check_coder() {
    local uuid="$1"
    local last_msg
    last_msg=$(last_assistant_message "${uuid}")

    local gate_hits
    gate_hits=$(echo "${last_msg}" | grep -ciP 'pre.?pr|quality.gate|linting|lint|tests?\s+pass|pytest|ruff|mypy' || echo 0)
    log "CODER: quality_gate_mentions=${gate_hits}"
    if [ "$gate_hits" -ge 1 ]; then
        echo "PASS"
    else
        echo "FAIL no quality gate mention in completion summary"
    fi
}

check_planner() {
    local uuid="$1"
    local last_msg
    last_msg=$(last_assistant_message "${uuid}")

    local plan_file
    plan_file=$(echo "${last_msg}" | grep -oP "[^\s'\"]+\.md" | head -1 || true)
    if [ -z "${plan_file}" ] || [ ! -f "${plan_file}" ]; then
        log "PLANNER: no plan file found in completion message (got: ${plan_file:-none})"
        echo "FAIL"
        return
    fi

    local da_hits
    da_hits=$(grep -ciP "^## Devil'?s Advocate" "${plan_file}" 2>/dev/null || echo 0)
    log "PLANNER: da_section=${da_hits} in ${plan_file}"
    if [ "$da_hits" -ge 1 ]; then
        echo "PASS"
    else
        echo "FAIL no DA section in ${plan_file}"
    fi
}

# ---------------------------------------------------------------------------
# Task bodies (roles injected inline — mirrors what delegate.md + research.md produce)
# ---------------------------------------------------------------------------

RESEARCHER_TASK='[JUGGLE_THREAD:<REPLACE>]
Research topic: "What are the latest 2026 best practices for prompt caching in Claude API?"

## Researcher behavioral spec

Mark confidence: [HIGH CONFIDENCE] (3+ independent sources) / [CONFLICTING] / [UNVERIFIED]
Never fabricate URLs. State gaps explicitly rather than guessing.

OUTPUT FORMAT:
## Summary (3-5 sentences, standalone readable)
## [Section per research angle]
  - Finding [CONFIDENCE] — URL
## Gaps / open questions

Intent: build
Focus areas: implementation, cost optimisation

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <THREAD> "Research complete: ~/Documents/personal/research/'"${SMOKE_DATE}"'-smoke-prompt-caching.md" --retain "<one-line finding>"'

CODER_TASK='[JUGGLE_THREAD:<REPLACE>]
Create the file /tmp/juggle_smoke_coder_'"${SMOKE_TS}"'.txt with the single line: smoke-test-ok

## Coder behavioral spec

SCOPE: Only create the one file above. No other changes.

QUALITY GATE (run before agent complete):
1. Verify the file exists and contains "smoke-test-ok"
2. Verify no other files were modified
3. Invoke mike:pre-pr skill if available; otherwise note "quality gate: file verified"

VERSION BUMP: n/a (no code change)

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <THREAD> "Created smoke file. Quality gate: file verified, content correct." --retain "smoke test"'

PLANNER_TASK='[JUGGLE_THREAD:<REPLACE>]
Plan adding a --dry-run flag to a hypothetical CLI command. Design exercise only — do not modify any real files except the plan output.

## Planner behavioral spec

DECOMPOSE: Break into subtasks of one file/concern each, ordered by dependency.
Each subtask must have: what to do, where to do it, acceptance criteria.

DEVIL'"'"'S ADVOCATE (mandatory before emitting plan):
1. Identify weakest assumption and its failure mode
2. Ask: is there a simpler alternative that achieves the same goal?
3. Hunt for hidden dependencies or scope creep
State findings in ## Devil'"'"'s Advocate section of plan.

DONE when: a coder with no prior context could execute every subtask without asking.

Save the plan to: /Users/mikechen/Documents/personal/projects/juggle/plan/'"${SMOKE_DATE}"'-smoke-dry-run-plan.md

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <THREAD> "Plan saved: /Users/mikechen/Documents/personal/projects/juggle/plan/'"${SMOKE_DATE}"'-smoke-dry-run-plan.md" --retain "dry-run flag plan"'

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    log "=== Juggle Agent Compliance Smoke Test ==="
    log "DB: ${DB_PATH}"
    log ""

    r_uuid="" r_result=""
    c_uuid="" c_result=""
    p_uuid="" p_result=""

    # Dispatch
    for role in researcher coder planner; do
        log "Dispatching ${role}..."
        case "${role}" in
            researcher) task="${RESEARCHER_TASK}" ;;
            coder)      task="${CODER_TASK}" ;;
            planner)    task="${PLANNER_TASK}" ;;
        esac
        info=$(dispatch_task "${role}" "${role}-smoke" "${task}" 2>/dev/null || echo "")
        if [ -z "${info}" ]; then
            log "SKIP: ${role} dispatch failed"
            case "${role}" in
                researcher) r_result="SKIP" ;;
                coder)      c_result="SKIP" ;;
                planner)    p_result="SKIP" ;;
            esac
            continue
        fi
        uuid="${info%%:*}"
        label="${info##*:}"
        log "  ${role} -> thread ${label} (${uuid})"
        case "${role}" in
            researcher) r_uuid="${uuid}" ;;
            coder)      c_uuid="${uuid}" ;;
            planner)    p_uuid="${uuid}" ;;
        esac
    done

    log ""
    log "Waiting for completions..."

    if [ -n "${r_uuid}" ]; then
        if wait_for_completion "${r_uuid}" "researcher"; then
            r_result=$(check_researcher "${r_uuid}")
        else
            r_result="TIMEOUT"
        fi
    fi
    if [ -n "${c_uuid}" ]; then
        if wait_for_completion "${c_uuid}" "coder"; then
            c_result=$(check_coder "${c_uuid}")
        else
            c_result="TIMEOUT"
        fi
    fi
    if [ -n "${p_uuid}" ]; then
        if wait_for_completion "${p_uuid}" "planner"; then
            p_result=$(check_planner "${p_uuid}")
        else
            p_result="TIMEOUT"
        fi
    fi

    log ""
    echo "========================================"
    echo "  Agent Compliance Smoke Test Results"
    echo "========================================"
    printf "  %-12s %s\n" "researcher:" "${r_result:-SKIP}"
    printf "  %-12s %s\n" "coder:"      "${c_result:-SKIP}"
    printf "  %-12s %s\n" "planner:"    "${p_result:-SKIP}"
    echo "========================================"

    failures=0
    for r in "${r_result:-SKIP}" "${c_result:-SKIP}" "${p_result:-SKIP}"; do
        [ "$r" = "PASS" ] || failures=$((failures + 1))
    done
    if [ "$failures" -eq 0 ]; then
        echo "  ALL PASS"
        exit 0
    else
        echo "  ${failures} role(s) FAILED or SKIPPED"
        exit 1
    fi
}

main "$@"
