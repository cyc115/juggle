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

Acknowledge:
```
Juggle active.
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

3. Dispatch background planning agent:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role planner
   # → <agent_id> <pane_id>
   cat > /tmp/juggle_task.txt << 'EOF'
   [JUGGLE_THREAD:<thread_id>]
   Write implementation plan for: <task description>
   Read relevant files. Write plan to /Users/mikechen/Documents/personal/projects/juggle/plan/<date>-<name>.md

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Written to <path>. Plan: • step1 • step2"
   EOF
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> /tmp/juggle_task.txt
   ```

4. Wait for user approval. Do not proceed without it.

### Phase 2 — Implement (background)

1. On approval, say:
   ```
   Implementing in background. Topic [X] running — what else?
   ```

2. Dispatch background implementation agent:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role coder
   # → <agent_id> <pane_id>
   cat > /tmp/juggle_task.txt << 'EOF'
   [JUGGLE_THREAD:<thread_id>]
   Implement approved plan:
   <paste approved plan bullets>

   Files: <list file paths>

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Done. <files changed>"
   EOF
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> /tmp/juggle_task.txt
   ```

3. On completion, notify at next natural pause:
   ```
   [Topic X done] <task label> — <1-line summary>. /juggle:resume-topic X to review.
   ```

---

## Research Protocol (Category 2)

1. Say: `"Researching in background..."`
2. Create topic thread.
3. Dispatch background research agent:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role researcher
   cat > /tmp/juggle_task.txt << 'EOF'
   [JUGGLE_THREAD:<thread_id>]
   <research question — specific files/question only>

   On completion:
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<findings summary>"
   EOF
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> /tmp/juggle_task.txt
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
```

**Dispatch pattern**:
```bash
# 1. Get best idle agent (spawns if needed)
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id> --role coder
# → <agent_id> <pane_id> [new]

# 2. Write task prompt to temp file
cat > /tmp/juggle_task.txt << 'EOF'
[JUGGLE_THREAD:<thread_id>]
<task: 1 line, imperative>

<context: files, constraints — only what agent needs>

On completion:
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<1-line result>"
EOF

# 3. Send to agent (appends release-agent automatically)
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> /tmp/juggle_task.txt
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
2. After responding: get a researcher agent and send the summarization task:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent <thread_id>
   cat > /tmp/summary_task.txt << 'EOF'
   [JUGGLE_THREAD:<thread_id>]
   Summarize thread. Run:
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-messages <thread_id> --plain --limit 10
   Write 1-2 telegraphic sentences (max 250 chars, no articles). Then:
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <thread_id> "<summary>"
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py set-summarized-count <thread_id> <count>
     python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "Done."
   EOF
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task <agent_id> /tmp/summary_task.txt
   ```
One dispatch per stale thread. No batching.
