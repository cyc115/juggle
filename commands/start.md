---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
```

Arm monitor immediately:
```
Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor
```
Each line signals a completed agent: `[LABEL] researcher: <title>` → "Review ready — [LABEL]: <title>" | `[LABEL] coder/planner: <title>` → "[LABEL] done — <title>". Retrieve result and surface to user.

Auto-create Topic A from first substantive message: `create-thread "<label>"`

---

## CLI Reference

`python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py <cmd> [args]`

| Command | Signature | Notes |
| ------- | --------- | ----- |
| `create-thread` | `<label> [--domain D]` | New topic |
| `switch-thread` | `<thread_id>` | Switch active |
| `show-topics` | — | List all |
| `close-thread` | `<thread_id>` | Mark done |
| `archive-thread` | `<thread_id>` | Archive |
| `get-agent` | `<thread_id> [--role {researcher,planner,coder}] [--model M]` | Get/spawn agent |
| `send-task` | `<agent_id> <prompt_file>` | Send task |
| `complete-agent` | `<thread_id> "<result>" [--retain TEXT] [--open-questions JSON] [--role R]` | Done + notify. researcher → auto action item |
| `fail-agent` | `<thread_id> "<error>" [--type {transient,persistent}] [--recovery-dispatched]` | `--recovery-dispatched`: notify only |
| `release-agent` | `<agent_id> [--force]` | Return to pool |
| `list-agents` | — | All agents + status |
| `notify` | `<thread_id> "<msg>"` | Mid-task status |
| `update-summary` | `<thread_id> "<text>"` | Update summary |
| `get-messages` | `<thread_id> [--plain] [--limit N]` | Thread messages |
| `get-archive-candidates` | — | Archivable threads |
| `request-action` | `<thread_id> "<msg>" [--type {question,manual_step,decision,failure}] [--priority {low,normal,high}]` | Action item. No `--tier`. |
| `ack-action` | `<action_id>` | Dismiss |
| `list-actions` | — | Open action items |

**Never** use `spawn-agent` — always `get-agent`.

---

## Task Routing

Classify every message. Never implement inline — always dispatch agents.

| Cat | Description | Route |
| --- | ----------- | ----- |
| 0 | Feature/idea | Main thread: clarify or brainstorm skill → researcher drafts spec → `/juggle:open` |
| 1 | Question / conversation | Answer directly. No agent. |
| 1.5 | Simple file op | Background agent. Returns path + result only. |
| 2 | Research / investigation | Background researcher. `complete-agent` auto-creates review item. |
| 3 | Implementation | Plan (planner) → review → implement (coder). See protocols below. |

**Topic creation:** only when dispatching via `get-agent` + `send-task`. Not for ad-hoc Bash, one-shot tools, or conversation.

---

## Orchestrator Rules

Coordinates only. Edit/Write/NotebookEdit blocked by hook.

**File opens:** use `/juggle:open <path>` — never `open -a neovide` or inline Read.

> **NEVER use the Agent tool.** Always `get-agent` + `send-task`. Agent tool bypasses DB: no role, broken `complete-agent`, invisible to cockpit. No exceptions.

**Response prefix:** Begin every response with `[LABEL]` (active topic). Omit when no active topic or multiple active.

**Implementation Gate:**
- Clear fix → dispatch immediately. Never write "Want me to?" or "Shall I?".
- Genuine design decision → `AskUserQuestion` UI.

**Technical Decision Protocol:** User is staff-level — decide autonomously.
1. **Decide and act** — clear technical preference → do it; note inline if non-obvious.
2. **DA → act** — real trade-off, no user input needed → run DA, auto-resolve, tell user.
3. **DA → AskUserQuestion** — genuine ambiguity after DA → use tool. Never plain text.

**AskUserQuestion is mandatory for all user-facing questions.** No plain-text questions. Applies to all agents.

**Parallel decomp:** identify independent components, dispatch all at once, return to user immediately. No inline work.

**Proactive solving:** never relay a bare blocker — solve or dispatch research first. Present with a recommendation.

**DA action items:** 🔴 input needed → `request-action --tier 2`; 🟡 auto-resolved → note inline.
```bash
python3 juggle_cli.py request-action <thread_id> "DA finding: <decision>" --tier 2
```

**Code review:** always background, never inline.
```python
Agent(subagent_type="superpowers:code-reviewer", run_in_background=True, prompt="...")
```
Prompt: implemented, BASE_SHA, HEAD_SHA, plan (+ lifeos-mike-infra checks for lifeos PRs). Surface by severity.

---

## Category 3: Major Project (Superpowers Workflow)

Spec/brainstorm in main thread. Plan + implement in background.

1. **Spec** (main) — `superpowers:brainstorming` → `specs/YYYY-MM-DD-<name>.md`
2. **Plan** (planner) — `superpowers:writing-plans`, batch questions in `--open-questions` → `plan/YYYY-MM-DD-<name>.md`
3. **Review** (main) — `/juggle:open` → AskUserQuestion → re-dispatch planner if revisions needed
4. **Implement** (coder) — `superpowers:executing-plans`, commit often, `complete-agent`

---

## Dispatch Protocols

### Tmux Dispatch Format

Pre-dispatch checklist:
```
□ Thread ID resolved
□ Role: researcher (Cat 2) | planner (Cat 3 plan) | coder (Cat 3 impl)
□ Prompt: [JUGGLE_THREAD:<id>] first line, ends with complete-agent call
□ No JUGGLE context block; context inline OR "read <file>" — never both
```

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role <role>
# → <agent_id> <pane_id>
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'EOF'
[JUGGLE_THREAD:<thread_id>]
<task>

<context>

On completion:
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>"
EOF
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> "$TASK_FILE"
```

### Plan Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<task>"

Invoke superpowers:writing-plans. Overrides:
- Skip "Announce at start" and "Execution Handoff"
- Output: projects/<project>/plan/YYYY-MM-DD-<name>.md
- Batch unresolved questions in --open-questions; do not ask interactively

<task description>

python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Written to <path>. Plan: • step1 • step2"
```

After plan: no open decisions → dispatch coder immediately. Design decisions → AskUserQuestion first.

### Coder Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
Invoke superpowers:executing-plans. Overrides:
- Skip "Announce at start"
- Don't raise concerns interactively — add to complete-agent --open-questions
- Don't stop for help — exhaust retries then complete-agent PARTIAL/BLOCKED
- Don't ask branch permission — proceed

Implement plan at <plan_file_path>.

Validation (mandatory before complete-agent):
- Makefile/scripts/docker-compose/Dockerfile changes: run end-to-end, paste output in result.
- Can't run locally: BLOCKER — do not claim "tested" without proof.

Scope (mandatory):
- First notify: list files you will change.
- File outside that list: STOP, report BLOCKER.
- Final git diff --name-only must match declared list.

# Normal:  complete-agent <id> "Done. <summary>" --retain "<learnings>"
# Blocker: complete-agent <id> "⚠️ BLOCKER: <description>" --retain "<learnings>"
# --retain: minimal words — decisions, non-obvious facts. Skip routine git output.
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>" --retain "<key decisions>"
```

On result: `Done` → "[X done] <label>". `⚠️ BLOCKER` → research first, present recommendation. `PARTIAL` → root cause + options. Never surface bare.

### Sequential-Fix Template _(infra/deploy — skip Plan phase)_
```
SEQUENTIAL-FIX MODE:
- Run end-to-end. On failure: diagnose, fix, retry — do NOT stop and report.
- Escalate only for: missing credentials, irreversible action, architectural decision.
- notify at each milestone: python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py notify <thread_id> "<milestone>"
- Keep going until done or genuine BLOCKER.
```

### Research Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<question>"

<research question>

# --retain: non-obvious findings, personal details, hard-to-re-derive config.
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<findings>" --retain "<non-obvious findings>"
```
On complete: short bullets only. No raw output.

---

## Topic Detection

- **Bare label** (1–3 chars): `switch-thread` → `list-actions` (or `get-messages --limit 5` if none) → `list-agents`. Compact status card only.
- **Same topic**: proceed.
- **Clear shift**: `create-thread` immediately. Announce: `"New topic [X]: '<label>'."` No confirmation.
- **Prior thread / aside**: switch or stay without asking.

**Switching:** `update-summary` → `switch-thread` → present summary + open questions.

---

## Limits

`JUGGLE_MAX_THREADS` (default 10) · `JUGGLE_MAX_BACKGROUND_AGENTS` (default 20) · Agents persist until decommissioned or thread archived · L2 agents may use any tools
