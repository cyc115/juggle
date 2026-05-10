---
name: delegate
description: Structured wizard to dispatch background agents — checklist → plan card → confirm → fire
allowed-tools: Bash, ToolSearch, AskUserQuestion
---

# /juggle:delegate — Dispatch Background Agents

Delegation wizard: 3-question checklist → plan card → one confirmation → agents fire.

**Usage:** `/juggle:delegate [task description]`

`$ARGUMENTS` contains the optional task description the user typed.

---

## Step 1: Collect task info

Load `AskUserQuestion` via ToolSearch (`select:AskUserQuestion`), then issue a **single call with 3 questions**:

- **Q1 — Deliverable** (`header: "Output type"`): What is the expected output?
  - `File (plan/spec/doc)` — writes a document to the vault or repo
  - `Code change (commit/PR)` — edits code files, commits
  - `Research summary` — investigates and surfaces findings
  - `Other` — (user will describe)

  If `$ARGUMENTS` is empty, prepend the task description ask: phrase Q1 as "Describe the task and expected output" so the user's Other response captures both.

- **Q2 — Constraints** (`header: "Constraints"`): Any scope, tech, or time limits?
  - `None`
  - `Scope-limited` — specific files, services, or directories
  - `Time-boxed` — complete within a fixed window
  - `Other` — (user will describe)

- **Q3 — Parallelism** (`header: "Parallelism"`): Can any parts run independently?
  - `No — single agent`
  - `Yes — researcher then coder` — sequential: researcher finds context, coder implements
  - `Yes — multiple coders` — parallel: split by scope

---

## Step 2: Derive plan parameters

From the answers:

**Thread label** — derive from `$ARGUMENTS` (if set) or Q1 answer: take first 4 words, lowercase, hyphen-separated. Example: "fix caller webhook signature check" → `caller-webhook-sig`.

**Agent role(s)**:
- Q1 = File → `planner`
- Q1 = Code change → `coder`
- Q1 = Research summary → `researcher`
- Q1 = Other → `researcher`
- Q3 = researcher then coder → two agents: `researcher` first, then `coder`
- Q3 = multiple coders → two `coder` agents with split scope

**Scope** — from Q2 answer (or "all files" if None).

---

## Step 3: Display plan card and confirm

Print a fenced plan card:

```
Thread [??]: <label>
Agents:
  • <role> → <scope>    (<sequential or parallel note>)
Output: <Q1 answer>
Constraints: <Q2 answer or "none">
```

Then load `AskUserQuestion` again and issue a **single confirmation question**:

- `header: "Dispatch"`
- Question: `"Fire this plan?"`
- Options:
  - `Yes — dispatch now`
  - `Cancel`

**On Cancel:** print `"Cancelled — no thread created."` and stop. Do not run any CLI commands.

---

## Step 4: Dispatch

Run the following Bash commands:

```bash
# 1. Create thread — capture label from output
CREATE_OUT=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<label>")
THREAD_LABEL=$(echo "$CREATE_OUT" | grep -oP '(?<=Created Topic )\w+')
echo "Thread: $THREAD_LABEL"
```

```bash
# 2. Get agent — first token is agent_id
AGENT_INFO=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent "$THREAD_LABEL" --role <role>)
AGENT_ID=$(echo "$AGENT_INFO" | awk '{print $1}')
echo "Agent: $AGENT_ID"
```

```bash
# 3. Write task prompt and dispatch
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'TASKEOF'
[JUGGLE_THREAD:<THREAD_LABEL>]
<task description from $ARGUMENTS or Q1 answer>

Constraints: <Q2 answer>

On completion:
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <THREAD_LABEL> "<1-line result>" --retain "<key decisions or findings>"
TASKEOF

python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task "$AGENT_ID" "$TASK_FILE"
```

Fill in the placeholders (`<label>`, `<role>`, `<THREAD_LABEL>`, task description, constraints) from the answers collected in Steps 1–2 before running.

### Parallel dispatch (Q3 = researcher then coder)

Run get-agent + send-task twice — once for researcher, once for coder — in the same response. Researcher task file is the research question. Coder task file says: "Researcher is running in parallel on [THREAD_LABEL]. Implement once you have context; check `get-messages` for researcher output."

### Parallel dispatch (Q3 = multiple coders)

Run get-agent + send-task for each coder. Split the scope from Q2 across the two coders (e.g., frontend vs backend, service A vs service B). Each task file contains its scoped subtask only.

### Pool exhausted

If `get-agent` exits non-zero or prints "Agent pool full": print `"Agent pool full — try again after an existing agent completes."` and stop. Do not retry.

---

## Step 5: Reminder

After every successful dispatch, end the response with:

> **Reminder:** orchestrator does no work on the main thread — all edits and reads go through agents.
