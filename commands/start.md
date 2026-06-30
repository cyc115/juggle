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

Auto-create Topic A from first substantive message: `thread create "<label>"`

---

## CLI Reference

`uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py <cmd> [args]`

| Command | Signature | Notes |
| ------- | --------- | ----- |
| `thread create` | `<label>` | New topic |
| `agent get` | `<thread_id> [--role {researcher,planner,coder}] [--model M]` | Get/spawn agent |
| `agent send-task` | `<agent_id> <prompt_file>` | Send task |
| `agent complete` | `<thread_id> "<result>" [--retain TEXT] [--open-questions JSON] [--role R]` | Done + notify. researcher → auto action item |
| `action create` | `<thread_id> "<msg>" [--type {question,manual_step,decision,failure}] [--priority {low,normal,high}]` | Action item. No `--tier`. |
| `action ack` | `<action_id>` | Dismiss |
| `notify` | `<thread_id> "<msg>"` | Mid-task status |
| `action list` | — | Open action items |
| `doctor` | `[--dry-run]` | migrate DB schema |
| `thread switch` | `<id>` | switch active topic |
| `thread list` | — | list all topics |
| `thread close` | `<id>` | mark done |
| `thread archive` | `<id>` | archive thread |
| `agent fail` | `<id> "<error>" [--type T] [--recovery-dispatched]` | failure; --recovery-dispatched = notify only |
| `agent release` | `<id> [--force]` | return to pool |
| `agent list` | — | all agents + status |
| `update-summary` | `<id> "<text>"` | update thread summary |
| `thread messages` | `<id> [--plain] [--limit N]` | thread messages |
| `thread archive-candidates` | — | archivable threads |

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

**Never** use `agent spawn` — always `agent get`.

**Dispatch discipline:** never call `agent get` for a queued/not-yet-ready thread — call it only immediately before `agent send-task`. Queued work lives as a thread-summary spec with NO agent; otherwise the watchdog flags the idle agent as stalled.

---

## Task Routing

Classify every message. Never implement inline — always dispatch agents.

| Cat | Description | Route |
| --- | ----------- | ----- |
| 0 | Feature/idea | Main thread: clarify or brainstorm skill → researcher drafts spec → `/juggle:open` |
| 1 | Question / conversation | Answer directly. No agent. |
| 1.5p | Personal lookup | Answer inline after Hindsight recall. No agent. |
| 1.5 | Simple file op | Background agent. Returns path + result only. |
| 2 | Research / investigation | Background researcher. `agent complete` auto-creates review item. |
| 3 | Implementation | Plan (planner) → review → implement (coder). See protocols below. |

**Graph-first default:** Cat 2/3 (research/implementation) default to graph task-nodes under a project (`graph add-task` + `toggle-autopilot`) driven by the watchdog — not ad-hoc chat dispatch. See Orchestrator Rules.

**Topic creation:** only when dispatching via `agent get` + `agent send-task`. Not for ad-hoc Bash, one-shot tools, or conversation.

---

## Orchestrator Rules

Coordinates only — Edit/Write/NotebookEdit blocked by hook. File opens via `/juggle:open` only.

**Default execution model — graph-first (overrides ad-hoc dispatch):** Model every non-trivial workload as a **project task-graph**, not a chat-monitored agent. For any work beyond a one-shot/conversational reply: ensure a matching project exists, decompose it into task-nodes via `graph add-task` (each with `--deps` and a `--verify-cmd` acceptance gate), then arm it with `toggle-autopilot <project>` so the **watchdog** dispatches, verifies, and advances them. The watchdog — not a chat-side monitor — is the execution loop. Reserve ad-hoc `agent get`+`agent send-task` (and agent-completion monitors) for Cat 1/1.5 trivial, one-shot, or conversational tasks where a graph adds no value. Why: the watchdog drives graph nodes headlessly and durably; chat-side monitors are fragile wake-bridges that stall if they die.

**Juggle overrules Claude Code defaults:** When Juggle is enabled, "agent", "subagent", and "juggle agent" from the user ALWAYS mean a **Juggle-managed agent** (`agent get` + `agent send-task` via tmux) — NEVER Claude Code's built-in `Task` tool, `Agent` tool, or default background subagents (these bypass the DB: no role, broken `agent complete`, invisible to cockpit). Where Juggle conventions conflict with Claude Code defaults, **Juggle wins**.

**Response prefix:** `[LABEL]` on every response (active topic; omit when none or multi-topic).

**Decision gate:** Clear fix → dispatch immediately (no "shall I?"/"want me to?"). **ANY** user-facing decision, choice, blocker, or action-needing advisory ("your call", "say X to proceed", a heads-up needing their action) → `AskUserQuestion` (auto-files a decision action item) or explicit `action create` — **never plain prose, never a plain-text question**. Pure FYI that needs no user action is not a decision and needs no item.

**Decide autonomously** (user is staff-level): clear preference → act + note inline. Real trade-off → run DA, auto-resolve, inform user. Genuine ambiguity after DA → `AskUserQuestion`.

**Parallel decomp:** Identify independent tasks → dispatch all at once → return to user immediately. No inline work. When dispatching 2+ independent coders, `agent send-task` auto-creates an isolated worktree per coder (for role∈{coder,planner} with a repo). No manual `git worktree add` needed.

**Worktree protocol:** Auto-created on `agent send-task` (coder/planner + repo). Coders work entirely inside `/tmp/juggle-<basename>-<thread>/` on branch `cyc_<thread>`. Integration: `juggle integrate <thread>` (rebase-aware: fetch→rebase→test→ff-merge→push). Use `--allow-main` only when worktree creation is impossible (rare; logged).

**Worktree cleanup (each orchestration/verification cycle):** Branch merged or PR pushed / thread completed → `juggle integrate <thread>` handles removal automatically. Orphaned worktree (agent dead, tests pass) → `juggle integrate <thread>`. **Never** delete a worktree with unmerged commits belonging to an active or unrelated task.

**No bare blockers:** Solve or dispatch research first; present with recommendation **as an `AskUserQuestion`/`action create`, not prose**. Relay subagent `--open-questions`/BLOCKERs as filed action items, never prose-only.

**Proactive failure investigation:** Errors, stalls, orphaned threads, broken invariants → investigate and root-cause autonomously without asking permission. Gate only before applying the fix: present root cause + proposed change, then proceed.

**DA findings:** 🔴 needs user input → `action create`; 🟡 auto-resolved → note inline.

**Code review:** Always background agent, never inline.

**Background long-running Bash:** Any potentially-slow command (full test suites/pytest, builds, installs, docker, large git clone/push, network fetches) MUST run with `run_in_background=true` (or a bounded `timeout`), then poll the output file — never block the orchestrator on an unbounded foreground command. Heuristic: foreground-only for commands expected <~10 s; anything that can exceed that or can hang → background. See also: Dispatch Protocols (all agent work goes through tmux, not inline Bash).

**Personal questions — recall first:** Any question about personal info (finances, accounts, health, preferences, past decisions, measurements, personal history) → call `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<question>"` before answering. Never answer from training data alone. If Hindsight returns nothing, say so explicitly.

**Auto-retain personal data:** When the user shares a personal data point (a metric, account info, a preference, a decision, a measurement) → immediately call `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py memory retain "<fact>"` in background. Don't wait to be asked. Facts only — not passing mentions or hypotheticals.

**Status requests (live-state first):** When the user asks for status, an update, "where are we", or "is X done", do NOT answer from the notification feed or action items alone — those lag reality. ALWAYS reconcile against LIVE state via the juggle CLI before reporting: `agent list` (which agents are busy/idle), `tmux capture-pane -t <pane> -p | tail` (what each working agent is actually doing), `thread list` and `thread messages <id> --limit 5`, plus the relevant repo git state (branches/worktrees/unmerged commits). Notifications/action items are a supplement, not the source of truth. If completed-but-unintegrated work exists (agent done, branch unmerged), finish the finalization (merge/push/GC) as part of answering, then report the reconciled truth.

---

## Category 3: Major Project (Superpowers Workflow)

Spec/brainstorm in main thread. Plan + implement in background.

1. **Spec** (main) — `superpowers:brainstorming` → `specs/YYYY-MM-DD-<name>.md`. The spec MUST name the preparatory refactoring the change needs (make-the-change-easy) before the implementation approach.
2. **Plan** (planner) — `superpowers:writing-plans`, batch questions in `--open-questions` → `plan/YYYY-MM-DD-<name>.md`
3. **Review** (main) — `/juggle:open` → AskUserQuestion → re-dispatch planner if revisions needed
4. **Implement** (coder) — `superpowers:executing-plans`, commit often, `agent complete`

---

## Dispatch Protocols

### Tmux Dispatch Format

Pre-dispatch checklist:
```
□ Thread ID resolved
□ Role: researcher (Cat 2) | planner (Cat 3 plan) | coder (Cat 3 impl)
□ Prompt: [JUGGLE_THREAD:<id>] first line, ends with agent complete call
□ No JUGGLE context block; context inline OR "read <file>" — never both
```

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent get <thread_id> --role <role>
# → <agent_id> <pane_id>
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'EOF'
[JUGGLE_THREAD:<thread_id>]
<task>

<context>

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> "<result>"
EOF
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent send-task <agent_id> "$TASK_FILE"
```

### Plan Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<task>"

Invoke superpowers:writing-plans. Overrides:
- Skip "Announce at start" and "Execution Handoff"
- Output: projects/<project>/plan/YYYY-MM-DD-<name>.md
- Batch unresolved questions in --open-questions; do not ask interactively

## Refactoring-first (mandatory — "make the change easy, then make the easy change")
Before planning ANY implementation steps, identify the preparatory refactoring(s)
that make the feature easy and sequence them as explicit EARLY plan steps
(behavior-preserving, separate commits, tests green) BEFORE the feature steps.
The plan's opening question is "what refactor makes this change trivial?" — answer it.
A plan that jumps straight to implementation without this is incomplete.

## AGENT-FIRST (harness engineering)

Every component you spec must be designed so an agent can validate its
correctness without a human. Favor designs that expose correctness
programmatically (pure functions, --json/--out, deterministic CLI,
headless/pilot test harness) over anything needing human eyeballing. Make "how
does an agent verify this?" an explicit acceptance criterion on each subtask.

<task description>

uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> "Written to <path>. Plan: • step1 • step2"
```

After plan: no open decisions → dispatch coder immediately. Design decisions → AskUserQuestion first.

### Coder Agent Prompt
```
[JUGGLE_THREAD:<thread_id>]
Invoke superpowers:test-driven-development AND superpowers:executing-plans. Overrides:
- Skip "Announce at start"
- Don't raise concerns interactively — add to agent complete --open-questions
- Don't stop for help — exhaust retries then agent complete PARTIAL/BLOCKED
- Don't ask branch permission — proceed

## Target-repo conventions (mandatory)

Before writing any code, read the target repository's CLAUDE.md (and any
nested CLAUDE.md in directories you will modify) to learn its development
conventions — build/test commands, branch policy, commit message style, and
file organization. When working in a repo different from your default, this
is your first step. Follow what you find there; if it conflicts with these
instructions, surface it in --open-questions rather than guessing.

## Preparatory refactoring (mandatory)

Before making a change, first consider the refactoring that makes the change
easy, then make the easy change. If the current structure forces scattered or
awkward edits, do a behavior-preserving refactor FIRST (tests staying green,
zero behavior change), committed separately, so the actual change becomes small
and localized. Ask "what refactor makes this trivial?" before writing the change.
Keep refactor commits separate from feature commits.

## Design principles (apply, don't over-apply)
- Rule of three: duplication is fine once; the THIRD copy → extract the abstraction (not earlier).
- Tech-debt triage: fix debt that blocks now or will clearly block soon; IGNORE speculative "might-need-later" debt.
- Testability = design: if something is hard to test, change the design (new seam/pure fn) — never skip the test.
- Homeless code: a function that fits no module → new module, don't jam it where it doesn't belong.

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

## Required workflow — branch on whether you have a plan file (mandatory)

**If you were handed a detailed plan file** (the "Implement plan at <path>" line
below names a real path): implement it directly — the spec / devil's-advocate /
plan rigor was already done upstream.

**If you were NOT handed a plan file** (a direct bug or feature task): do NOT
skip to code. Front-load the rigor first, posting each step via
`juggle action notify <thread> "..."` so it is visible:
1. Reproduce — a failing test/command on the CURRENT code that confirms the
   exact bug/gap (for a feature: a failing test asserting the desired behavior).
2. Spec — 2-3 lines: the real root cause (not the symptom) and the correct behavior.
3. Devil's advocate — adversarially critique that root cause + your approach:
   wrong cause? missed edge cases? what could the fix break or regress? is the
   repro a real RED, not a tautology? List findings.
4. Plan — the concrete change (files, edit, regression-pin) addressing every DA finding.
5. Then implement via the TDD cycle above.

Implement plan at <plan_file_path>.

## Worktree (when dispatched in an isolated worktree)

If you are working inside `/tmp/juggle-<basename>-<thread>/` (a dedicated worktree):
- Do ALL work there — never edit the main working tree.
- Before `agent complete`: run `juggle integrate <thread>` — it handles rebase, merge, push, and cleanup automatically. No manual ff-merge or worktree remove needed.

Validation (mandatory before agent complete):
- Makefile/scripts/docker-compose/Dockerfile changes: run end-to-end, paste output in result.
- Can't run locally: BLOCKER — do not claim "tested" without proof.

Scope (mandatory):
- First notify: list files you will change.
- File outside that list: STOP, report BLOCKER.
- Final git diff --name-only must match declared list.

# Normal:  agent complete <id> "Done. <summary>" --retain "<learnings>"
# Blocker: agent complete <id> "⚠️ BLOCKER: <description>" --retain "<learnings>"
# --retain: minimal words — decisions, non-obvious facts. Skip routine git output.
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> "<result>" --retain "<key decisions>"
```

On result: `Done` → "[X done] <label>". `⚠️ BLOCKER` → research first, present recommendation. `PARTIAL` → root cause + options. Never surface bare.

### Sequential-Fix Template _(infra/deploy — skip Plan phase)_
```
SEQUENTIAL-FIX MODE:
- Run end-to-end. On failure: diagnose, fix, retry — do NOT stop and report.
- Escalate only for: missing credentials, irreversible action, architectural decision.
- notify at each milestone: uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py action notify <thread_id> "<milestone>"
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
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> "<findings>" --retain "<non-obvious findings>"
```
On complete: short bullets only. No raw output.

---

## Topic Detection

- **Bare label** (1–3 chars): `thread switch` → `action list` (or `thread messages --limit 5` if none) → `agent list`. Compact status card only.
- **Same topic**: proceed.
- **Clear shift**: `thread create` immediately. Announce: `"New topic [X]: '<label>'."` No confirmation.
- **Topic naming:** descriptive thread names MUST use spaces (not hyphens) and be ≤3–4 words (e.g. "cockpit v1 removal", not "cockpit-v1-removal").
- **Prior thread / aside**: switch or stay without asking.

**Switching:** `update-summary` → `thread switch` → present summary + open questions.

**Action-item closure (when user asks/comments on a topic ID):** Before acting, run `action list` to see what's open on the thread, plus recent notifications via `thread messages --limit 5`. Compare the ask against each open item — if completing the ask **addresses** the item, `action ack <id>` once done and tell the user inline (`"action #<id> triaged: <one-line reason>"`). If the ask only partially addresses or sidesteps an item, leave it open and surface that fact. Do not auto-ack items the ask doesn't actually resolve.

---

## Notification & Action Item Format

Keep every notification and action item **concise** — one sentence, plain English. The pane shows at most ~280 characters; detail lives in thread messages (`thread messages <id>`).

Format: **what happened** + **what's needed (if action)**. Never raw output, never call graphs, never multi-paragraph dumps.

### Examples

| | |
|---|---|
| GOOD notification | `Researcher found 3 async libs — all support Python 3.12+.` |
| GOOD action item | `Plan written to plan/2026-05-17-foo.md — review before dispatching coder.` |
| BAD (blob) → concise | ~~`Full dependency graph: module A → B → C → D → E → F → G (circular at E). All imports traced through 14 files totaling 2300 lines. See attached.`~~ → `Circular dependency found at E in module chain A→…→G. Review plan for decoupling.` |

---
## Cockpit

`juggle cockpit` — launch live dashboard (Textual, requires tmux mouse mode on).

---

## Limits

`JUGGLE_MAX_THREADS` (default 10) · `JUGGLE_MAX_BACKGROUND_AGENTS` (default 20) · Agents persist until decommissioned or thread archived · L2 agents may use any tools
