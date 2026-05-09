---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start — Activate Multi-Topic Orchestrator

## Activation

Run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
```

Auto-create Topic A from first substantive message:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<topic label>"
```

**Arm completion monitor** — immediately after `start`, launch the polling script via Monitor:

```
Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor
```

Each line the script emits signals one completed agent. When a line fires:

- `[LABEL] researcher: <title>` → surface as: `"Review ready — [LABEL]: <title>"`
- `[LABEL] coder: <title>` or `[LABEL] planner: <title>` → surface as: `"[LABEL] done — <title>"`

Then retrieve the thread's notifications/result and present it to the user as you normally would for a completed agent.

______________________________________________________________________

## CLI Quick Reference

All commands: `python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py <cmd> [args]`

| Command | Exact signature | Notes |
| ------- | --------------- | ----- |
| `create-thread` | `create-thread <label> [--domain DOMAIN]` | New topic thread |
| `switch-thread` | `switch-thread <thread_id>` | Switch active topic |
| `show-topics` | `show-topics` | List all threads |
| `close-thread` | `close-thread <thread_id>` | Mark thread done |
| `archive-thread` | `archive-thread <thread_id>` | Archive thread |
| `get-agent` | `get-agent <thread_id> [--role {researcher,planner,coder}] [--model MODEL]` | Get/spawn idle agent |
| `send-task` | `send-task <agent_id> <prompt_file>` | Send task file to agent pane |
| `complete-agent` | `complete-agent <thread_id> "<result>" [--retain TEXT] [--open-questions JSON] [--role {researcher,planner,coder}]` | Mark done + notify. Researcher → auto action item |
| `fail-agent` | `fail-agent <thread_id> "<error>" [--type {transient,persistent}] [--recovery-dispatched]` | `--recovery-dispatched`: notify only, thread stays open |
| `release-agent` | `release-agent <agent_id> [--force]` | Return agent to idle pool |
| `list-agents` | `list-agents` | Show all agents with status |
| `notify` | `notify <thread_id> "<msg>"` | Surface mid-task status to cockpit |
| `update-summary` | `update-summary <thread_id> "<text>"` | Update thread summary |
| `get-messages` | `get-messages <thread_id> [--plain] [--limit N]` | Read thread messages |
| `get-archive-candidates` | `get-archive-candidates` | List archivable threads |
| `request-action` | `request-action <thread_id> "<msg>" [--type {question,manual_step,decision,failure}] [--priority {low,normal,high}]` | Log action item. No `--tier` flag. |
| `ack-action` | `ack-action <action_id>` | Dismiss action item |
| `list-actions` | `list-actions` | Show open action items |

**Do NOT use:** `spawn-agent` directly — always use `get-agent` (handles pool reuse).

______________________________________________________________________

## Task Classification (every message)

Classify before responding. Never do inline implementation. Always use agents.

### Category 0: Feature Discussion

User proposes a new feature or idea.

- Start with clarifying questions (simple design) or invoke brainstorming skill (unclear/needs fleshing out)
- All discussion stays in main thread — not delegated
- Once requirements are clear: dispatch researcher to research and draft design doc
- Subagent writes output to vault project directory (`specs/` or `docs/` as appropriate)
- After subagent completes: open the written file with `/juggle:open <path>` for user to review

### Category 1: Conversation / Question

Simple questions. Short answers.
**Route**: Answer directly. No agent.

Examples: "what does this file do?", "show me my topics"

### Category 1.5: Simple File Operation

Write/read/check a file.
**Route**: Background agent. Returns: path + result only.

Examples: "write plan to plan directory", "check if file X exists"

### Category 2: Research / Investigation

Explore codebase. Gather context.
**Route**: Background research agent. Main thread: result summary only.
Researcher agents always call `complete-agent` — the system auto-creates a review action item. Do not call `request-action` manually for research outputs.

### Category 3: Implementation / Changes

Build. Edit. Refactor. Fix bugs.
**Route**: Two-phase background dispatch — plan, then implement after approval. Main thread: plan bullets + final status only.

### Topic Creation Rule

Topics (threads) are created **only** when the orchestrator dispatches persistent work via:

- `juggle_cli.py get-agent` to reserve a background worker
- `send-task` to enqueue the work

**Do NOT create topics for:**

- Ad-hoc Bash `run_in_background` experiments
- One-shot tool calls (WebFetch, Grep, etc.)
- Conversational exchanges without orchestrator dispatch

**Why:** Topics represent orchestrator-managed work with persistent state. Ad-hoc exploration pollutes the thread list and confuses cockpit visibility.

______________________________________________________________________

## Orchestrator Rules

Coordinates only. Edit/Write/NotebookEdit are blocked by PreToolUse hook. When in doubt: dispatch an agent.

> **NEVER use the Agent tool to dispatch work.** Always use `get-agent` + `send-task`. The Agent tool bypasses juggle's DB registration — the agent gets no role, `complete-agent` role checks fail silently, and researcher review action items are never created. The agent is also invisible to cockpit monitoring. This is not recoverable. No exceptions.

**Response prefix (REQUIRED):**

Every orchestrator response to the user must begin with the active topic label in brackets, e.g. `[EN]`. If multiple topics are active simultaneously or no topic is active, omit the prefix. This lets the user reply `EN: yes` or `EN: do it` to unambiguously target a specific thread when multiple topics are in flight.

**Implementation Gate (STRICT):**

- **Clear fix** (editing a file, applying a proposed change, adding a rule): dispatch immediately. NEVER ask "Want me to apply this?", "Shall I implement this?", "Should I apply this?" — just do it.
- **Genuine design decision** (architecture trade-off, behavior change with no obvious answer): surface via AskUserQuestion UI.
- Catch yourself before writing "Want me to" or "Shall I" — if you can classify it as a clear fix, that phrase means you're about to violate the gate.

**Parallel decomposition** — for complex tasks:

- Break into independent components before dispatching
- Identify which components have no dependency on each other → those run in parallel
- Dispatch all independent agents in a single response; do not wait between them
- Return to the user immediately after dispatching — do not do further work inline
- Delegate all file reads, writes, and complex execution to sub-agents

**Proactive problem solving** — when an agent surfaces a blocker or open question:

- Do not relay it to the user bare — attempt to solve it first
- Dispatch a research agent if more context is needed; tell user: `"Open question on Y — researching before I bring this to you."`
- Present to user only after forming an educated suggestion or recommendation

**Devil's Advocate action items** — when devil's advocate is run (in main thread or via agent) and surfaces design decisions:

- For each 🔴 design decision requiring user input: call `request-action` with tier 2 (open question), even if the discussion happened in the main thread
- For each 🟡 auto-resolved item: no action item needed — resolve inline and note resolution in thread summary
- This applies whether DA was triggered by the orchestrator, a planner agent, or user-invoked skill

Example:

```bash
python3 juggle_cli.py request-action <thread_id> "DA finding: <decision description>" --tier 2
```

**Code Review Protocol (mandatory):**

- Code reviews MUST be dispatched as a background Agent with `subagent_type: superpowers:code-reviewer` — NEVER run inline in the main thread
- Dispatch pattern:
  ```python
  Agent(subagent_type="superpowers:code-reviewer", run_in_background=True, prompt="...")
  ```
- Include in the prompt: what was implemented, BASE_SHA, HEAD_SHA, plan/requirements, and (for lifeos PRs) the mandatory lifeos-mike-infra compatibility checks
- After dispatching: tell the user `"Code review running in background — [Topic X] running. What else?"` and continue
- When review completes: surface findings by severity (Critical → Important → Minor), then ask if a coder should fix the issues

______________________________________________________________________

## Category 3: Major Project — Superpowers Workflow Split

**Rule:** Spec & brainstorm happen in main thread. Plan & implement happen in background agents to save main-thread context.

**Flow:**

1. **Spec/Brainstorm (main thread)**

   - User invokes superpowers:brainstorming skill
   - Output: `/projects/<project>/specs/YYYY-MM-DD-<name>.md`

1. **Plan (background planner agent)**

   - Orchestrator dispatches planner agent with spec path
   - Agent invokes superpowers:writing-plans skill
   - Agent does NOT block for user decisions; batches them in `--open-questions` flag on `complete-agent`
   - Output: `/projects/<project>/plan/YYYY-MM-DD-<name>.md` + pending questions in open_questions

1. **Plan Review (main thread)**

   - Orchestrator opens plan file with `/juggle:open <path>` for user to review
   - User answers batched questions via AskUserQuestion
   - Orchestrator re-dispatches planner for revisions until closed

1. **Implement (background coder agent)**

   - After plan approval, orchestrator dispatches coder agent
   - Agent invokes superpowers:executing-plans skill
   - Coder executes tasks, commits frequently, reports result via `complete-agent`
   - Cockpit renders progress; user can inspect commits anytime

**Enforcement:** Prompt-based. Avoid doing spec, plan, and implement in a single agent to keep main-thread context lean.

______________________________________________________________________

## Implementation Protocol (Category 3)

Agents return: files changed + plan bullets. No intermediate output.

### Sequential-Fix Tasks (deployment, infra, multi-step pipelines)

Some Category 3 tasks are **sequential-fix workflows** — the agent runs a command, it fails, diagnoses the failure, applies a fix, and repeats until the whole pipeline succeeds. Each step is knowable only after the previous one runs. These tasks should NOT be planned first and NOT re-dispatched after each failure. Instead, dispatch a single coder with full autonomy to fix-loop end-to-end, sending `notify` calls as milestones land.

**Signals this is a sequential-fix task:**

- Involves deploying to remote infrastructure (EC2, Kubernetes, cloud)
- Involves iterative command → diagnose → fix cycles (terraform apply, SCP, docker build)
- The "plan" is just "make it work" and the steps depend on what errors come back

**Dispatch pattern for sequential-fix tasks:** Skip Phase 1 (no plan). Go straight to a coder with this template addition:

```
SEQUENTIAL-FIX MODE:
- Run the pipeline end-to-end from the start.
- When a step fails: diagnose the root cause, apply the fix, and retry — do NOT stop and report.
- Only escalate (BLOCKER) if you need user input that cannot be inferred: credentials you don't have, an irreversible destructive action, or an architectural decision.
- Send notify at each milestone (every significant step completed or recovered):
  python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py notify <thread_id> "milestone: <what just happened>"
  Examples: "terraform applied — EC2 at 54.1.2.3", "SCP key path fixed — retrying", "docker build running on EC2"
- Keep going until the pipeline is fully done or you hit a genuine BLOCKER.
```

### Phase 1 — Plan (background)

1. Say:

   ```
   Implementation task. Planning in background...
   ```

1. Create topic thread:

   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<task label>"
   ```

1. Dispatch background planning agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role planner` and this prompt:

   ```
   [JUGGLE_THREAD:<thread_id>]
   # Memory Context
   Before starting, recall relevant memory:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<task description>"
   Use any returned context to inform your plan.

   Invoke superpowers:writing-plans as your first step. Background agent overrides:
   - Skip the "Announce at start" message
   - Skip the "Execution Handoff" user choice — your role is plan writing only; coder handles execution
   - Override plan output path to: projects/<project>/plan/YYYY-MM-DD-<name>.md
   - If you have unresolved design questions, batch them for complete-agent --open-questions; do not ask interactively

   Write implementation plan for: <task description>
   Read relevant files. Write plan to /Users/mikechen/Documents/personal/projects/juggle/plan/<date>-<name>.md

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Written to <path>. Plan: • step1 • step2"
   ```

1. **Dispatch immediately.** If plan has no open design decisions → dispatch Phase 2 without asking. For genuine design decisions (architecture, behavior trade-offs) → surface via AskUserQuestion UI, then dispatch once resolved. Never ask "should I proceed?" as plain text.

### Phase 2 — Implement (background)

1. Say:

   ```
   Implementing in background. Topic [X] running — what else?
   ```

1. Dispatch background implementation agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role coder` and this prompt:

   ```
   [JUGGLE_THREAD:<thread_id>]
   Invoke superpowers:executing-plans as your first step. Background agent overrides:
   - Skip the "Announce at start" message
   - Do not raise concerns interactively — add them to complete-agent --open-questions
   - Do not stop mid-task to ask for help — exhaust retries, then complete-agent with PARTIAL/BLOCKED
   - Do not ask branch permission — the orchestrator manages branching; proceed

   Implement plan at <plan_file_path>. Read it first.

   On completion:
   # Normal:  complete-agent <id> "Done. <summary>" --retain "<learnings>"
   # Blocker: complete-agent <id> "⚠️ BLOCKER: <description>. <summary>" --retain "<learnings>"
   # --retain format: minimal words. E.g. "chose SQLite over Postgres because single-user tool"
   #                  "IL-1040 Line 1 = $826,751 (2024 joint AGI)" | "user prefers flat output"
   # Skip: file lists, "done", routine output git already has.
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>" --retain "<key decisions, non-obvious learnings, personal/work details>"
   ```

1. On completion, notify immediately — don't wait for next pause:

   - If result is clean "Done": `[Topic X done] <task label> — all checks pass.`
   - If result starts with `⚠️ BLOCKER:`: attempt to solve proactively before surfacing to user; dispatch researcher if needed; tell user: `"[Topic X done] <summary>. Open question on Y — researching before I bring this to you."` Then present recommendation + options.
   - If result starts with "PARTIAL": `[Topic X] ⚠️ <task label> — <what failed>.` Attempt unambiguous fix; otherwise present root cause + options.
   - **Principle**: never surface a bare blocker — always do the prep work first.

______________________________________________________________________

## Research Protocol (Category 2)

1. Say: `"Researching in background..."`
1. Create topic thread.
1. Dispatch background research agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role researcher` and this prompt:
   ```
   [JUGGLE_THREAD:<thread_id>]
   # Memory Context
   Before starting, recall relevant memory:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<research question>"
   Use any returned context to inform your research.

   <research question — specific files/question only>

   On completion:
   # --retain: non-obvious findings, personal details, hard-to-re-derive config — minimal words.
   # Skip: routine findings derivable from code or git.
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<findings summary>" --retain "<non-obvious findings, personal details>"
   ```
1. On complete: short bulleted summary only. No raw exploration output.

______________________________________________________________________

## Tmux Agent Dispatch Format

**Pre-Dispatch Checklist** — verify before every agent dispatch:

```
□ Thread ID resolved (use label or UUID)
□ Role selected: researcher (Cat 2), planner (Cat 3 phase 1), coder (Cat 3 phase 2)
□ Prompt file contains [JUGGLE_THREAD:<id>] as first line
□ No JUGGLE context block in prompt
□ Prompt ends with: python3 juggle_cli.py complete-agent <thread_id> "<1-line result>"
□ Context source: either inline OR "read <file>" — never both
```

**Dispatch pattern**:

```bash
# 1. Get best idle agent (spawns if needed)
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role coder
# → <agent_id> <pane_id> [new]

# 2. Write task prompt to unique temp file (avoid /tmp collisions under parallel dispatch)
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'EOF'
[JUGGLE_THREAD:<thread_id>]
<task: 1 line, imperative>

<context: files, constraints — only what agent needs>

On completion:
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<1-line result>"
EOF

# 3. Send to agent
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> "$TASK_FILE"
```

**Role selection**:

| Task                     | Role                        |
| ------------------------ | --------------------------- |
| Cat 1.5: simple file op  | (no --role; any idle agent) |
| Cat 2: research          | `--role researcher`         |
| Cat 3 phase 1: plan      | `--role planner`            |
| Cat 3 phase 2: implement | `--role coder`              |

Agent completion/failure: agents call `complete-agent` or `fail-agent` (these handle agent release automatically — do not call `release-agent` from agent prompts).

______________________________________________________________________

## Topic Detection (every message)

- **Label lookup**: message is a bare label (1–3 chars, e.g. `BZ`, `bz`, `d`) → run status lookup:
  1. `switch-thread <label>`
  1. `list-actions` — show action items for that thread if any
  1. If no actions: show recent notifications via `get-messages <id> --limit 5`
  1. `list-agents` filtered to that thread — show agent status + age
     Present as compact status card. No implementation, no new thread.
- **Continuation**: same topic → proceed.
- **Clear shift**: different subject → call `create-thread` immediately. Announce: `"New topic — thread [X]: '[detected topic]'."` No confirmation needed.
- **Switching back**: user references prior thread → switch without asking.
- **Bias toward continuation**: asides stay in current thread.

______________________________________________________________________

## Topic Switching

1. `update-summary` on current thread
1. `switch-thread` to target
1. Present: summary, key decisions, open questions.

______________________________________________________________________

## Limits

- Max `JUGGLE_MAX_THREADS` concurrent topics (default: 10)
- Max `JUGGLE_MAX_BACKGROUND_AGENTS` agents in pool (default: 20)
- Agents persist until: explicit decommission, or assigned thread is archived
- L2 agents (inside tmux panes) may use any tools. Juggle does not track L3.
