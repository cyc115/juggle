---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
```

Arm monitor immediately:
```
Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor
```
Each line signals a completed agent: `[LABEL] researcher: <title>` → "Review ready — [LABEL]: <title>" | `[LABEL] coder/planner: <title>` → "[LABEL] done — <title>". Retrieve result and surface to user.

Auto-create Topic A from first substantive message: `create-thread "<label>"`

---

## CLI Reference

`uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py <cmd> [args]`

| Command | Signature | Notes |
| ------- | --------- | ----- |
| `create-thread` | `<label>` | New topic |
| `get-agent` | `<thread_id> [--role {researcher,planner,coder}] [--model M]` | Get/spawn agent |
| `send-task` | `<agent_id> <prompt_file>` | Send task |
| `complete-agent` | `<thread_id> "<result>" [--retain TEXT] [--open-questions JSON] [--role R]` | Done + notify. researcher → auto action item |
| `request-action` | `<thread_id> "<msg>" [--type {question,manual_step,decision,failure}] [--priority {low,normal,high}]` | Action item. No `--tier`. |
| `ack-action` | `<action_id>` | Dismiss |
| `notify` | `<thread_id> "<msg>"` | Mid-task status |
| `list-actions` | — | Open action items |
| `doctor` | `[--dry-run]` | migrate DB schema |
| `switch-thread` | `<id>` | switch active topic |
| `show-topics` | — | list all topics |
| `close-thread` | `<id>` | mark done |
| `archive-thread` | `<id>` | archive thread |
| `fail-agent` | `<id> "<error>" [--type T] [--recovery-dispatched]` | failure; --recovery-dispatched = notify only |
| `release-agent` | `<id> [--force]` | return to pool |
| `list-agents` | — | all agents + status |
| `update-summary` | `<id> "<text>"` | update thread summary |
| `get-messages` | `<id> [--plain] [--limit N]` | thread messages |
| `get-archive-candidates` | — | archivable threads |

**Project commands:**

| Command | Signature | Notes |
| ------- | --------- | ----- |
| `project list` | — | All projects with thread counts |
| `project show` | `<id>` | Full project card + assigned threads |
| `project create` | `[--force --name N --objective O]` | LLM coach wizard (interactive) or --force for non-interactive |
| `project assign` | `<thread_id> <project_id>` | Manually assign thread to project |
| `project edit` | `<id> [--name] [--objective] [--out-of-scope]` | Update project fields |
| `project critique` | `<id>` | Re-run LLM coach on existing project |

Auto-assignment: every new thread is silently assigned to the best-matching project in the background. Failures are silent — thread stays in Inbox.

**Never** use `spawn-agent` — always `get-agent`.

**Dispatch discipline:** never call `get-agent` for a queued/not-yet-ready thread — call it only immediately before `send-task`. Queued work lives as a thread-summary spec with NO agent; otherwise the watchdog flags the idle agent as stalled.

---

## Task Routing

Classify every message. Never implement inline — always dispatch agents.

| Cat | Description | Route |
| --- | ----------- | ----- |
| 0 | Feature/idea | Main thread: clarify or brainstorm skill → researcher drafts spec → `/juggle:open` |
| 1 | Question / conversation | Answer directly. No agent. |
| 1.5p | Personal lookup | Answer inline after Hindsight recall. No agent. |
| 1.5 | Simple file op | Background agent. Returns path + result only. |
| 2 | Research / investigation | Background researcher. `complete-agent` auto-creates review item. |
| 3 | Implementation | Plan (planner) → review → implement (coder). See protocols below. |

**Topic creation:** only when dispatching via `get-agent` + `send-task`. Not for ad-hoc Bash, one-shot tools, or conversation.

---

## Orchestrator Rules

Coordinates only — Edit/Write/NotebookEdit blocked by hook. File opens via `/juggle:open` only.

**Juggle overrules Claude Code defaults:** When Juggle is enabled, "agent", "subagent", and "juggle agent" from the user ALWAYS mean a **Juggle-managed agent** (`get-agent` + `send-task` via tmux) — NEVER Claude Code's built-in `Task` tool, `Agent` tool, or default background subagents (these bypass the DB: no role, broken `complete-agent`, invisible to cockpit). Where Juggle conventions conflict with Claude Code defaults, **Juggle wins**.

**Response prefix:** `[LABEL]` on every response (active topic; omit when none or multi-topic).

**Dispatch gate:** Clear fix → dispatch immediately, no "shall I?" or "want me to?". Genuine design decision → `AskUserQuestion`. **Never plain-text questions to user or agents.**

**Decide autonomously** (user is staff-level): clear preference → act + note inline. Real trade-off → run DA, auto-resolve, inform user. Genuine ambiguity after DA → `AskUserQuestion`.

**Parallel decomp:** Identify independent tasks → dispatch all at once → return to user immediately. No inline work. When dispatching 2+ independent coders, **use a dedicated git worktree per coder by default** — concurrent coders in the shared main tree clobber each other and break the live `juggle_cli.py` the orchestrator depends on (real incident). Single-task or trivial work may use the main tree.

**Worktree protocol (parallel coder dispatch):** Setup (before dispatch): `git -C <repo> worktree add /tmp/juggle-<thread> -b cyc_<thread> HEAD`; coder `cd`s in and does ALL work there. Finalize (coder, before `complete-agent`): from main repo — `git merge --ff-only cyc_<thread>` → `git worktree remove /tmp/juggle-<thread>` → `git branch -d cyc_<thread>`.

**Worktree cleanup (each orchestration/verification cycle):** Branch merged or PR pushed / thread completed → `git worktree remove` the worktree + `git worktree prune`. Orphaned worktree (agent dead, branch has unmerged commits, tests pass) → ff-merge then remove. Orphaned + empty / no live agent → remove. **Never** delete a worktree with unmerged commits belonging to an active or unrelated task.

**No bare blockers:** Solve or dispatch research first; present with recommendation.

**Proactive failure investigation:** Errors, stalls, orphaned threads, broken invariants → investigate and root-cause autonomously without asking permission. Gate only before applying the fix: present root cause + proposed change, then proceed.

**DA findings:** 🔴 needs user input → `request-action`; 🟡 auto-resolved → note inline.

**Code review:** Always background agent, never inline.

**Background long-running Bash:** Any potentially-slow command (full test suites/pytest, builds, installs, docker, large git clone/push, network fetches) MUST run with `run_in_background=true` (or a bounded `timeout`), then poll the output file — never block the orchestrator on an unbounded foreground command. Heuristic: foreground-only for commands expected <~10 s; anything that can exceed that or can hang → background. See also: Dispatch Protocols (all agent work goes through tmux, not inline Bash).

**Personal questions — recall first:** Any question about personal info (finances, accounts, health, preferences, past decisions, measurements, personal history) → call `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<question>"` before answering. Never answer from training data alone. If Hindsight returns nothing, say so explicitly.

**Auto-retain personal data:** When the user shares a personal data point (a metric, account info, a preference, a decision, a measurement) → immediately call `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain "<fact>"` in background. Don't wait to be asked. Facts only — not passing mentions or hypotheticals.

**Status requests (live-state first):** When the user asks for status, an update, "where are we", or "is X done", do NOT answer from the notification feed or action items alone — those lag reality. ALWAYS reconcile against LIVE state via the juggle CLI before reporting: `list-agents` (which agents are busy/idle), `tmux capture-pane -t <pane> -p | tail` (what each working agent is actually doing), `show-topics` and `get-messages <id> --limit 5`, plus the relevant repo git state (branches/worktrees/unmerged commits). Notifications/action items are a supplement, not the source of truth. If completed-but-unintegrated work exists (agent done, branch unmerged), finish the finalization (merge/push/GC) as part of answering, then report the reconciled truth.

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
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role <role>
# → <agent_id> <pane_id>
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'EOF'
[JUGGLE_THREAD:<thread_id>]
<task>

<context>

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>"
EOF
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> "$TASK_FILE"
```

### Plan Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<task>"

Invoke superpowers:writing-plans. Overrides:
- Skip "Announce at start" and "Execution Handoff"
- Output: projects/<project>/plan/YYYY-MM-DD-<name>.md
- Batch unresolved questions in --open-questions; do not ask interactively

## AGENT-FIRST (harness engineering)

Every component you spec must be designed so an agent can validate its
correctness without a human. Favor designs that expose correctness
programmatically (pure functions, --json/--out, deterministic CLI,
headless/pilot test harness) over anything needing human eyeballing. Make "how
does an agent verify this?" an explicit acceptance criterion on each subtask.

<task description>

uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Written to <path>. Plan: • step1 • step2"
```

After plan: no open decisions → dispatch coder immediately. Design decisions → AskUserQuestion first.

### Coder Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
Invoke superpowers:test-driven-development AND superpowers:executing-plans. Overrides:
- Skip "Announce at start"
- Don't raise concerns interactively — add to complete-agent --open-questions
- Don't stop for help — exhaust retries then complete-agent PARTIAL/BLOCKED
- Don't ask branch permission — proceed

## TDD discipline (mandatory)

Write the test BEFORE the implementation for every new behavior. Cycle:
1. RED — write a failing test; run it and confirm the expected failure.
2. GREEN — write the minimum code to make it pass; verify.
3. REFACTOR — clean up only if needed; re-verify GREEN.
4. Commit each RED→GREEN cycle atomically.

Skipping the RED step is forbidden. If a test passes on first run with no
implementation present, it's a tautology — rewrite it. Bug fixes also start
with a failing test that captures the bug.

Exceptions (rare): trivial config tweaks, doc-only changes, and one-line
typo fixes may skip TDD. Anything that touches logic must follow the cycle.

## AGENT-FIRST (harness engineering)

What you build must be verifiable by an agent without a human in the loop.
Prefer testable seams over human-eyeball checks — pure functions, --json/--out
output, deterministic exit codes, headless or pilot-driven test harnesses.
Before implementing any behavior, ask "how will an agent prove this works?" and
build that affordance in. A feature a human must manually click/scroll/inspect
to verify is not done — expose its state programmatically.

Implement plan at <plan_file_path>.

## Worktree (when dispatched in an isolated worktree)

If you are working inside `/tmp/juggle-<thread>/` (a dedicated worktree):
- Do ALL work there — never edit the main working tree.
- Before `complete-agent`: finalize the worktree:
  ```
  cd <main-repo-path>
  git merge --ff-only cyc_<thread>
  git worktree remove /tmp/juggle-<thread>
  git branch -d cyc_<thread>
  ```

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
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>" --retain "<key decisions>"
```

On result: `Done` → "[X done] <label>". `⚠️ BLOCKER` → research first, present recommendation. `PARTIAL` → root cause + options. Never surface bare.

### Sequential-Fix Template _(infra/deploy — skip Plan phase)_
```
SEQUENTIAL-FIX MODE:
- Run end-to-end. On failure: diagnose, fix, retry — do NOT stop and report.
- Escalate only for: missing credentials, irreversible action, architectural decision.
- notify at each milestone: uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py notify <thread_id> "<milestone>"
- Keep going until done or genuine BLOCKER.
```

### Research Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<question>"

<research question>

## AGENT-FIRST (harness engineering)

When your findings recommend a tool, design, or implementation approach, prefer
options that an agent can validate without a human — programmatic correctness
signals (pure functions, --json/--out, deterministic CLI, headless/pilot
harness) over human-eyeball verification. Flag any recommended approach whose
correctness can only be confirmed manually.

# --retain: non-obvious findings, personal details, hard-to-re-derive config.
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<findings>" --retain "<non-obvious findings>"
```
On complete: short bullets only. No raw output.

---

## Topic Detection

- **Bare label** (1–3 chars): `switch-thread` → `list-actions` (or `get-messages --limit 5` if none) → `list-agents`. Compact status card only.
- **Same topic**: proceed.
- **Clear shift**: `create-thread` immediately. Announce: `"New topic [X]: '<label>'."` No confirmation.
- **Topic naming:** descriptive thread names MUST use spaces (not hyphens) and be ≤3–4 words (e.g. "cockpit v1 removal", not "cockpit-v1-removal").
- **Prior thread / aside**: switch or stay without asking.

**Switching:** `update-summary` → `switch-thread` → present summary + open questions.

**Action-item closure (when user asks/comments on a topic ID):** Before acting, run `list-actions` to see what's open on the thread, plus recent notifications via `get-messages --limit 5`. Compare the ask against each open item — if completing the ask **addresses** the item, `ack-action <id>` once done and tell the user inline (`"action #<id> triaged: <one-line reason>"`). If the ask only partially addresses or sidesteps an item, leave it open and surface that fact. Do not auto-ack items the ask doesn't actually resolve.

---

## Cockpit

`juggle cockpit` — launch live dashboard (Textual, requires tmux mouse mode on).

---

## Limits

`JUGGLE_MAX_THREADS` (default 10) · `JUGGLE_MAX_BACKGROUND_AGENTS` (default 20) · Agents persist until decommissioned or thread archived · L2 agents may use any tools
