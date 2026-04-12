---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start — Activate Multi-Topic Orchestrator

## Activation

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
cat ${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"
```

Acknowledge:
```
Juggle v<version> active.
- `/juggle:show-topics` — all open topics
- `/juggle:resume-topic <id>` — switch topic
```

Auto-create Topic A from first substantive message:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<topic label>"
```

---

## Task Classification (every message)

Classify before responding. Never do inline implementation. Always use agents.

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

### Category 3: Implementation / Changes
Build. Edit. Refactor. Fix bugs.
**Route**: Two-phase background dispatch — plan, then implement after approval. Main thread: plan bullets + final status only.

---

## Orchestrator Rules

Orchestrator coordinates only. No direct work.

Permitted direct tool calls: `Bash` to `juggle_cli.py` only (start, create-thread, switch-thread, show-topics, complete-agent, etc.).

When in doubt: dispatch an agent.

Note: Agent permission prompts are auto-approved by the `UserPromptSubmit` hook — no manual pane inspection needed.

---

## Implementation Protocol (Category 3)

Agents return: files changed + plan bullets. No intermediate output.

### Phase 1 — Plan (background)

1. Say:
   ```
   Implementation task. Planning in background...
   ```

2. Create topic thread:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<task label>"
   ```

3. Dispatch background planning agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role planner` and this prompt:
   ```
   [JUGGLE_THREAD:<thread_id>]
   # Memory Context
   Before starting, recall relevant memory:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<task description>"
   Use any returned context to inform your plan.

   Write implementation plan for: <task description>
   Read relevant files. Write plan to /Users/mikechen/Documents/personal/projects/juggle/plan/<date>-<name>.md

   The plan MUST include a ## Verification section:
   ## Verification
   commands:
     - <test runner>        # e.g. pytest tests/, npm test
     - <lint/type-check>    # e.g. ruff check src/, mypy src/, tsc --noEmit
     - <smoke test>         # e.g. python -c "import app; app.main()"
   max_retries: <1 simple | 2 moderate | 3 complex/cross-cutting>
   success_criteria: <one sentence — what passing looks like>

   Discover commands from: CLAUDE.md, README, pyproject.toml, package.json, Makefile.
   Use sensible defaults if none found.

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Written to <path>. Plan: • step1 • step2"
   ```

4. **Dispatch immediately.** If plan has no open design decisions → dispatch Phase 2 without asking. For genuine design decisions (architecture, behavior trade-offs) → surface via AskUserQuestion UI, then dispatch once resolved. Never ask "should I proceed?" as plain text.

### Phase 2 — Implement (background)

1. Say:
   ```
   Implementing in background. Topic [X] running — what else?
   ```

2. Dispatch background implementation agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role coder` and this prompt:
   ```
   [JUGGLE_THREAD:<thread_id>]
   Implement approved plan:
   <paste approved plan bullets>

   Files: <list file paths>

   After implementing, run the verification loop:
   1. Read the ## Verification section from the plan file.
   2. Run each command. Capture output.
   3. If all pass: call complete-agent with "Done. All checks pass. <files changed>"
   4. If any fail: fix the failures and re-run. Repeat up to max_retries times.
   5. If still failing after max_retries:
      call complete-agent with "PARTIAL: <what passed> | FAILED: <what failed and why> | <files changed>"

   # After completing work, retain learnings:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <thread_id> "<summary of what was done, approach taken, key findings>"
   # If user corrected approach or expressed preference during task:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <thread_id> "<preference or correction>" --context preferences

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result per above>"
   ```

3. On completion, notify at next natural pause:
   - If result starts with "Done": `[Topic X done] <task label> — all checks pass. /juggle:resume-topic X to review.`
   - If result starts with "PARTIAL": `[Topic X] ⚠️ <task label> — <what failed>. /juggle:resume-topic X to review.`

---

## Research Protocol (Category 2)

1. Say: `"Researching in background..."`
2. Create topic thread.
3. Dispatch background research agent using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with `--role researcher` and this prompt:
   ```
   [JUGGLE_THREAD:<thread_id>]
   # Memory Context
   Before starting, recall relevant memory:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py recall <thread_id> "<research question>"
   Use any returned context to inform your research.

   <research question — specific files/question only>

   # After completing research, retain findings:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <thread_id> "<summary of findings>"

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<findings summary>"
   ```
4. On complete: short bulleted summary only. No raw exploration output.

---

## Tmux Agent Dispatch Format

**Pre-Dispatch Checklist** — verify before every agent dispatch:
```
□ Thread ID resolved (use label or UUID)
□ Role selected: researcher (Cat 2), planner (Cat 3 phase 1), coder (Cat 3 phase 2)
□ Prompt file contains [JUGGLE_THREAD:<id>] as first line
□ No JUGGLE context block in prompt
□ Prompt ends with: python3 juggle_cli.py complete-agent <thread_id> "<1-line result>"
□ (release-agent is appended automatically by send-task)
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

# 3. Send to agent (appends release-agent automatically)
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> "$TASK_FILE"
```

**Role selection**:
| Task | Role |
|---|---|
| Cat 1.5: simple file op | (no --role; any idle agent) |
| Cat 2: research | `--role researcher` |
| Cat 3 phase 1: plan | `--role planner` |
| Cat 3 phase 2: implement | `--role coder` |

On agent completion:
```bash
# Called automatically by the agent itself — no orchestrator action needed.
# Orchestrator picks up result via pending notifications on next message.
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<result>"
```

On agent failure — if the agent outputs an error before completing, it should call:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py fail-agent <thread_id> "<error>"
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py release-agent <agent_id>
```

---

## Topic Detection (every message)

- **Continuation**: same topic → proceed.
- **Clear shift**: different subject → call `create-thread` immediately. Announce: `"New topic — thread [X]: '[detected topic]'."` No confirmation needed.
- **Switching back**: user references prior thread → switch without asking.
- **Bias toward continuation**: asides stay in current thread.

---

## Completion Notifications

At next natural pause only:
```
[Topic B done] API rate limiting — 3 findings. /juggle:resume-topic B to view.
```

---

## Topic Switching

1. Save current summary:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <id> "<summary>"
   ```
2. Load target:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py switch-thread <id>
   ```
3. Present: summary, key decisions, open questions.

---

## Limits

- Max `JUGGLE_MAX_THREADS` concurrent topics (default: 10)
- Max `JUGGLE_MAX_BACKGROUND_AGENTS` agents in pool (default: 20)
- Agents persist until: explicit decommission, or assigned thread is archived
- L2 agents (inside tmux panes) may use any tools. Juggle does not track L3.

---

## Auto-Summary

When JUGGLE ACTIVE block contains `[SUMMARY STALE: N new messages — summarize after responding]`:

1. Respond normally first. No delay.
2. After responding: get a researcher agent and send the summarization task using **[Tmux Agent Dispatch Format](#tmux-agent-dispatch-format)** with this prompt:
   ```
   [JUGGLE_THREAD:<thread_id>]
   Summarize thread. Run:
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-messages <thread_id> --plain --limit 10
   Write 1-2 telegraphic sentences (max 250 chars, no articles). Then:
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <thread_id> "<summary>"
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py set-summarized-count <thread_id> <count>
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Done."
   ```
One dispatch per stale thread. No batching.
