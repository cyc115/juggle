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

3. Dispatch background planning agent with:
   - Full task description
   - Relevant context (files, constraints)
   - Return format only:
     ```
     Written to <relative/path/to/plan.md>.

     Plan:
     • [step 1]
     • [step 2]
     • [step 3]
     ```

4. Wait for user approval. Do not proceed without it.

### Phase 2 — Implement (background)

1. On approval, say:
   ```
   Implementing in background. Topic [X] running — what else?
   ```

2. Dispatch background implementation agent with:
   - Approved plan
   - File paths
   - Return format: files changed + blockers only

3. On completion, notify at next natural pause:
   ```
   [Topic X done] <task label> — <1-line summary>. /juggle:resume-topic X to review.
   ```

---

## Research Protocol (Category 2)

1. Say: `"Researching in background..."`
2. Create topic thread. Dispatch background research agent.
3. On complete: short bulleted summary only. No raw exploration output.

---

## Background Agent Dispatch Format

**Pre-Dispatch Checklist** — verify before every `Agent(run_in_background=True)`:
```
□ First line: [JUGGLE_THREAD:<id>]
□ No JUGGLE context block in prompt
□ Each line: "would agent fail without this?" — if no, cut it
□ Task: 1 line, imperative ("Fix X in Y")
□ Compact headers: "Edit both:" not "Files to edit (both must be updated):"
□ Use "Find:" / "Replace:" not verbose descriptions
□ No transitional phrases ("After editing...", "Then run:")
□ No conversation history or unrelated agent results
□ Output format specified
```

**Compact format example**:
```
[JUGGLE_THREAD:<id>]
Fix summary style in auto-summary prompt.
Edit both:
- /path/to/file.py
- /cache/path/file.py

Find: `old string`
Replace: `new string`

git add file.py && git commit -m "fix: description"
python3 juggle_cli.py complete-agent <id> "<result>"

Output: files changed + commit hash.
```

Scoping by phase:
- **Phase 1 (plan)**: Task-relevant files only. Use `get-shared-context --type decision` for cross-thread decisions.
- **Phase 2 (implement)**: Approved plan bullets + file paths. Nothing else.
- **Research**: Specific files/question only. No JUGGLE block.

All background agents:
- `run_in_background: true`
- Clear output format

On agent completion:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> "<concise result>"
```

On agent failure:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py fail-agent <thread_id> "<error>"
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
- Max `JUGGLE_MAX_BACKGROUND_AGENTS` concurrent agents (default: 20)
- Agent timeout: 15 minutes
- L2 agents may spawn unlimited subagents. Juggle does not track L3.

---

## Auto-Summary

When JUGGLE ACTIVE block contains `[SUMMARY STALE: N new messages — summarize after responding]`:

1. Respond normally first. No delay.
2. After responding: spawn Haiku background agent per stale thread. Prompt: `[JUGGLE_THREAD:<id>]` — fetch last 10 messages via `get-messages --plain --limit 10`, write 1-2 telegraphic sentences (max 250 chars, no articles), call `update-summary` and `set-summarized-count`. Output: "Done. No prose."

Use `model: haiku`. One agent per stale thread. No batching.
