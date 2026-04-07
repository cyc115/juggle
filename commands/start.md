---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start — Activate Multi-Topic Orchestrator

When the user runs `/juggle:start`, activate juggle mode for the rest of this conversation session.

## What to Do

1. **Initialize the backend**:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
   ```

2. **Acknowledge activation** briefly:
   ```
   Juggle mode activated.
   - Talk normally — I'll route tasks to background agents and keep this thread clean
   - `/juggle:show-topics` — see all open topics
   - `/juggle:resume-topic <id>` — switch topics
   ```

3. **Auto-create Topic A** from the first substantive message:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<topic label>"
   ```

---

## Task Classification (apply on EVERY user message)

Before responding to any message, classify it into one of three categories and route accordingly. **Never do inline implementation work — always use agents.**

### Category 1: Conversation / Question
Simple questions, clarifications, status checks, short answers.
**Route**: Answer directly in main thread. No agent needed.

Examples: "what does this file do?", "what's the weather?", "show me my topics"

### Category 1.5: Simple File Operation
Writing a plan file, reading a config, checking a file exists, writing a doc.
**Route**: Background agent. Agent performs the operation and returns only: path + 1-line description.

Examples: "write plan to the plan directory", "check if file X exists", "write this to a doc"

### Category 2: Research / Investigation
Understand a codebase, explore options, read files, gather context.
**Route**: Dispatch a background research agent. Main thread only shows the result summary.

### Category 3: Implementation / Changes
Build something, edit files, refactor, create a plugin, write code, fix bugs.
**Route**: Two-phase background dispatch — plan first, implement after approval. Main thread only sees plan bullets and final status. Never sees file reads, edits, or intermediate steps.

---

## Orchestrator Rules — The Orchestrator Never Does Work

The orchestrator is a coordinator only. These rules are non-negotiable:

- **NEVER** use Read, Write, Edit, Glob, Grep, or Bash directly (except `juggle_cli.py` state commands)
- **NEVER** read files for context inline — send an agent to read and summarize
- **NEVER** write plan files inline — send an agent to write and report back
- **NEVER** show file contents, diffs, tool output, or bash output in the main thread
- **NEVER** perform research inline — research always goes to an agent
- **NEVER dispatch a foreground agent** — every Agent call MUST use `run_in_background: true`. The orchestrator must remain responsive to user input at all times. No exceptions.

The only direct tool calls permitted are `Bash` calls to `juggle_cli.py` for backend state management (start, create-thread, switch-thread, show-topics, complete-agent, etc.).

**Violating these rules defeats the purpose of Juggle.** When in doubt: dispatch an agent.

---

## Implementation Task Protocol (Category 3)

When the user asks for any implementation work, follow this exact sequence:

### Phase 1 — Plan (background)

1. Say:
   ```
   This looks like an implementation task. Planning in background...
   ```

2. Create a new topic thread for this task:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<task label>"
   ```

3. Dispatch a **background planning agent** with:
   - The full task description
   - All relevant context (current files, existing code, constraints)
   - Instruction to read relevant files, write the plan file to the appropriate path, and return ONLY:
     ```
     Written to <relative/path/to/plan.md>.

     Plan:
     • [step 1]
     • [step 2]
     • [step 3]
     ```
   - Tag `[JUGGLE_THREAD:<thread_id>]` in the prompt so the hook links it

4. When the planning agent completes, the orchestrator surfaces ONLY the path + bullet list:
   ```
   Plan ready — [task label]

   Written to <relative/path/to/plan.md>.

   • [step 1]
   • [step 2]
   • [step 3]
   ...

   Approve to implement, or say what to change.
   ```

5. Wait for user approval. Do not proceed until the user explicitly approves.

### Phase 2 — Implement (background)

1. On approval, say:
   ```
   Implementing in background. Topic [X] is running — what else are you working on?
   ```

2. Dispatch a **background implementation agent** with:
   - The approved plan
   - All context needed to execute each step
   - Instruction to make all changes and report back only: files changed + any blockers
   - Tag `[JUGGLE_THREAD:<thread_id>]` in the prompt

3. When the implementation agent completes, notify at the next natural pause:
   ```
   [Topic X done] <task label> — <1-line summary of what changed>. Use /juggle:resume-topic X to review.
   ```

### Main Thread Rules (all categories)

- **NEVER** use Read, Write, Edit, Glob, Grep, or Bash in the main thread (except juggle_cli.py)
- **NEVER** read files inline during implementation
- **NEVER** show file diffs, edit blocks, or bash output in the main thread
- **NEVER** ask clarifying questions mid-implementation — gather all context before dispatching
- The main thread should only ever contain: task acknowledged → plan bullets → approved → running → done

---

## Research Task Protocol (Category 2)

1. Say: `"Researching in background..."`
2. Create a topic thread and dispatch a background research agent
3. When complete, surface only the key findings as a short bulleted summary
4. Never show the raw exploration output in the main thread

---

## Background Agent Dispatch Format

**Pre-Dispatch Checklist** — verify every item before calling Agent(run_in_background=True):
```
□ First line of prompt: [JUGGLE_THREAD:<id>]
□ No "--- JUGGLE ACTIVE ---" or any JUGGLE context block in the prompt
□ Each line passes: "would the agent fail without this?" — if no, cut it
□ No conversation history, thread summaries, or unrelated agent results
□ Output format specified (bullets, no file dumps)
```

Scoping rules by phase:
- **Phase 1 (plan)**: Only files/snippets for this task. Use `get-shared-context --type decision` for cross-thread facts.
- **Phase 2 (implement)**: Approved plan bullets + file paths. Nothing else.
- **Research**: Specific files/question only. No JUGGLE block.

All background agents must be dispatched with:
- Tag `[JUGGLE_THREAD:<thread_id>]` as the first line of the prompt (required for hook to link it)
- `run_in_background: true`
- Clear instruction on output format: concise bullets only, no verbose narration

When the agent finishes, call:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<concise result summary>"
```

If the agent fails:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py fail-agent <thread_id> "<error description>"
```

---

## Topic Detection

On every user message, also check for topic shifts:
- **Continuation**: relates to current topic → proceed normally
- **Clear shift**: substantially different subject → **immediately** call create-thread CLI and announce: `"New topic — created thread [X] for '[detected topic]'."` Do NOT ask for confirmation. Creating a thread is low-risk and reversible. Just do it.
- **Switching back**: if user references a previous thread explicitly, switch to it without asking
- **Bias toward continuation**: asides and brief questions stay in current thread

---

## Completion Notifications

Show at the next natural pause, not mid-conversation:
```
[Topic B done] API rate limiting analysis ready — 3 findings. /juggle:resume-topic B to view.
```

---

## Topic Switching

When switching (via `/juggle:resume-topic` or implicit):
1. Save current topic summary to DB:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <id> "<summary>"
   ```
2. Load target topic:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py switch-thread <id>
   ```
3. Present loaded context concisely — summary, key decisions, open questions

---

## Limits
- Max `JUGGLE_MAX_THREADS` concurrent topics (default: 10, set via env var)
- Max `JUGGLE_MAX_BACKGROUND_AGENTS` concurrent background agents (default: 20, set via env var)
- Agent timeout: 15 minutes
- L2 agents may spawn unlimited subagents internally. Juggle does not track or limit L3 agents.

---

## Auto-Summary

When the JUGGLE ACTIVE block contains `[SUMMARY STALE: N new messages — summarize after responding]`:

1. Complete your response to the user normally — do not delay
2. After responding, spawn a Haiku background agent:

**Pre-dispatch checklist applies.** Strip all JUGGLE context from the agent prompt.

Agent prompt template:
```
[JUGGLE_THREAD:<thread_id>]
Task: Refresh thread summary.
Current summary: <paste existing summary, or "none">
Messages (run this and paste output):
  python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-messages <thread_id> --plain --limit 10

Write 1-2 sentences updating the summary. Telegraphic style: no articles, simple words, incomplete sentences fine. Cover: decided, built, open.
Run:
  python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <thread_id> "<new summary>"
  python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py set-summarized-count <thread_id> <msg_count from get-stale-threads>
Output: Done. No prose.
```

Use `model: haiku`. One summarizer per stale thread — do not batch into a single agent.
