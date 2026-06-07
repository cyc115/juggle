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

## Step 1.5: Context exploration (orchestrator-inline, before dispatch)

Follow the **CODEBASE block** in [`commands/_context-extraction.md`](_context-extraction.md). Use the task description from `$ARGUMENTS` or Q1 as the input. Assign the result to variable `CONTEXT_SUMMARY`.

This summary is injected into the task file (step 4) under `## Context from codebase`.

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

## Step 3: Display plan card and dispatch

Print a fenced plan card:

```
Thread [??]: <label>
Agents:
  • <role> → <scope>    (<sequential or parallel note>)
Output: <Q1 answer>
Constraints: <Q2 answer or "none">
```

**Determine whether to auto-dispatch or confirm:**

**Auto-dispatch** (print `"Auto-dispatching..."` then proceed to Step 4) if ALL of:
- Q1 answer is NOT "Other" — OR — Q1 is "Other" but `$ARGUMENTS` contains a non-empty task description or the answer text is meaningful
- Q2 answer is not empty
- Q3 answer is not empty

**Confirm first** (load `AskUserQuestion` and ask before dispatching) if ANY of:
- Q1 = "Other" with no task description in `$ARGUMENTS` and no meaningful answer text
- The task scope is unclear based on answers

When confirming, issue a **single confirmation question**:
- `header: "Dispatch"`
- Question: `"Fire this plan?"`
- Options:
  - `Yes — dispatch now`
  - `Cancel`

**On Cancel** (either from confirmation or if `$ARGUMENTS` contains an explicit cancellation intent like `cancel` before the wizard runs): print `"Cancelled — no thread created."` and stop. Do not run any CLI commands.

---

## Step 4: Dispatch

Run the following Bash commands:

```bash
# 1. Create thread — capture label from output
CREATE_OUT=$(uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<label>")
THREAD_LABEL=$(echo "$CREATE_OUT" | grep -oP '(?<=Created Topic )\w+')
echo "Thread: $THREAD_LABEL"
```

```bash
# 2. Get agent — first token is agent_id
AGENT_INFO=$(uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent "$THREAD_LABEL" --role <role>)
AGENT_ID=$(echo "$AGENT_INFO" | awk '{print $1}')
echo "Agent: $AGENT_ID"
```

```bash
# 3. Write task prompt and dispatch
TASK_FILE="/tmp/juggle_task_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'TASKEOF'
[JUGGLE_THREAD:<THREAD_LABEL>]
<task description from $ARGUMENTS or Q1 answer>

<BEHAVIORAL_SPEC>

## Context from codebase
<CONTEXT_SUMMARY>

Use this context to avoid re-exploring known ground. Trust it as the state at dispatch time.

Constraints: <Q2 answer>

On completion:
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <THREAD_LABEL> "<1-line result>" --retain "<key decisions or findings>"
TASKEOF

uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task "$AGENT_ID" "$TASK_FILE"
```

Fill in the placeholders (`<label>`, `<role>`, `<THREAD_LABEL>`, task description, constraints, `<BEHAVIORAL_SPEC>`) from the answers collected in Steps 1–2 before running. Substitute `<BEHAVIORAL_SPEC>` with the role-appropriate block from the templates below.

### Behavioral Spec Templates

**Coder** (role = `coder`):
```
## Coder behavioral spec

## Invoke TDD by default

Invoke `superpowers:test-driven-development` before implementation. Cycle:
1. RED — write a failing test; run it and confirm failure mode.
2. GREEN — minimum code to pass; verify.
3. REFACTOR if necessary; re-verify.
4. Commit each RED→GREEN atomically.

Skipping RED is forbidden. Trivial config/doc/typo edits may skip; logic
changes must follow the cycle.

## Always finalize — never wait at the prompt

Your task ENDS with a `complete-agent` or `fail-agent` Bash call. You must
NOT emit a final recap and wait at the input prompt. Use the juggle_cli
path from your AGENT ROLE block's COMPLETION line (written `<juggle-cli>`
below) — do not hardcode an absolute path. Examples of correct final actions:

- Work done cleanly:
  <juggle-cli> complete-agent <THREAD> "Done. <summary>" --retain "<notes>" --role <role>

- Hit a wall:
  <juggle-cli> complete-agent <THREAD> "⚠️ BLOCKER: <description>" --retain "<context>" --role <role>

- Have unresolved questions:
  <juggle-cli> complete-agent <THREAD> "Done with caveats. See open questions." --open-questions '[{"q": "...", "context": "..."}]' --role <role>

Do NOT ask the user "want me to commit?" or "shall I proceed?" — decide
autonomously from the task spec. The orchestrator already approved scope
when it dispatched you.

## Pre-existing failures are not your concern

If the full test suite has failures unrelated to your diff:

1. Confirm via `git stash && pytest <failing-test> && git stash pop` that
   the failure exists on the base commit too.
2. If yes: it's pre-existing. Continue. Note it in --retain.
3. If no: it's a regression in your change. Fix it.

Do NOT deliberate for minutes on whether to fix unrelated failures. The
DA gate is for unrelated failures.

SCOPE: Only change what the task requires. Do not refactor, add comments, or improve
surrounding code. If requirements are ambiguous, STOP and signal via complete-agent
with "BLOCKED: <question>" before making assumptions.

QUALITY GATE (run before complete-agent):
1. Run tests for changed files (if tests exist)
2. Fix linting errors
3. Fix type errors
4. Verify diff has no unrelated changes
5. Invoke mike:pre-pr skill (configurable via agent.quality_gate_skill setting)

VERSION BUMP: patch=fix, minor=feature, major=breaking. State target version in summary.

## AGENT-FIRST (harness engineering)

What you build must be verifiable by an agent without a human in the loop.
Prefer testable seams over human-eyeball checks — pure functions, --json/--out
output, deterministic exit codes, headless or pilot-driven test harnesses.
Before implementing any behavior, ask "how will an agent prove this works?" and
build that affordance in. A feature a human must manually click/scroll/inspect
to verify is not done — expose its state programmatically.

## Worktree (when dispatched in an isolated worktree)

If you are working inside `/tmp/juggle-<basename>-<thread>/` (a dedicated worktree):
- Do ALL work there — never edit the main working tree.
- Before `complete-agent`: run `juggle integrate <thread>` — handles rebase, merge, push, and cleanup automatically. No manual ff-merge or worktree remove needed.
```

**Planner** (role = `planner`):
```
## Planner behavioral spec

## Always finalize — never wait at the prompt

Your task ENDS with a `complete-agent` or `fail-agent` Bash call. You must
NOT emit a final recap and wait at the input prompt. Use the juggle_cli
path from your AGENT ROLE block's COMPLETION line (written `<juggle-cli>`
below) — do not hardcode an absolute path. Examples of correct final actions:

- Work done cleanly:
  <juggle-cli> complete-agent <THREAD> "Done. <summary>" --retain "<notes>" --role <role>

- Hit a wall:
  <juggle-cli> complete-agent <THREAD> "⚠️ BLOCKER: <description>" --retain "<context>" --role <role>

- Have unresolved questions:
  <juggle-cli> complete-agent <THREAD> "Done with caveats. See open questions." --open-questions '[{"q": "...", "context": "..."}]' --role <role>

Do NOT ask the user "want me to commit?" or "shall I proceed?" — decide
autonomously from the task spec. The orchestrator already approved scope
when it dispatched you.

## Pre-existing failures are not your concern

If the full test suite has failures unrelated to your diff:

1. Confirm via `git stash && pytest <failing-test> && git stash pop` that
   the failure exists on the base commit too.
2. If yes: it's pre-existing. Continue. Note it in --retain.
3. If no: it's a regression in your change. Fix it.

Do NOT deliberate for minutes on whether to fix unrelated failures. The
DA gate is for unrelated failures.

DECOMPOSE: Break into subtasks of one file/concern each, ordered by dependency.
Each subtask must have: what to do, where to do it, acceptance criteria.

DEVIL'S ADVOCATE (mandatory before emitting plan):
1. Identify weakest assumption and its failure mode
2. Ask: is there a simpler alternative that achieves the same goal?
3. Hunt for hidden dependencies or scope creep
State findings in ## Devil's Advocate section of plan.

DONE when: a coder with no prior context could execute every subtask without asking.

## AGENT-FIRST (harness engineering)

Every component you spec must be designed so an agent can validate its
correctness without a human. Favor designs that expose correctness
programmatically (pure functions, --json/--out, deterministic CLI,
headless/pilot test harness) over anything needing human eyeballing. Make "how
does an agent verify this?" an explicit acceptance criterion on each subtask.
```

**Researcher** (role = `researcher`): omit `<BEHAVIORAL_SPEC>` — use `/juggle:research` which embeds the spec automatically.

### Parallel dispatch (Q3 = researcher then coder)

Run get-agent + send-task twice — once for researcher, once for coder — in the same response. Researcher task file is the research question. Coder task file says: "Researcher is running in parallel on [THREAD_LABEL]. Implement once you have context; check `get-messages` for researcher output."

### Parallel dispatch (Q3 = multiple coders)

Run get-agent + send-task for each coder. Split the scope from Q2 across the two coders (e.g., frontend vs backend, service A vs service B). Each task file contains its scoped subtask only.

**Worktree isolation (auto for parallel coders):** `send-task` auto-creates `/tmp/juggle-<basename>-<thread>/` on branch `cyc_<thread>` for role∈{coder,planner} with a repo. No manual `git worktree add` step. The worktree path and branch are injected into the agent's prompt automatically. Coders finalize with `juggle integrate <thread>` — no manual merge/remove.

### Pool exhausted

If `get-agent` exits non-zero or prints "Agent pool full": print `"Agent pool full — try again after an existing agent completes."` and stop. Do not retry.

---

## Step 5: Reminder

After every successful dispatch, end the response with:

> **Reminder:** orchestrator does no work on the main thread — all edits and reads go through agents.
