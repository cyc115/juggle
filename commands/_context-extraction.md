# Shared partial: orchestrator-side context extraction for dispatched agents

This partial is referenced by dispatching skills (`/juggle:research`, `/juggle:delegate`). It defines the canonical prose for extracting context from the orchestrator's current session and injecting it into the agent's task file.

It is **not** invoked at runtime — dispatching skills inline its instructions by reference. Update this file when the extraction pattern changes, and the dispatching skills inherit the change.

> **Note on `juggle_context.py`:** The `UserPromptSubmit` hook (`src/juggle_context.py`) auto-injects DB-persisted thread state (other open topics, action items, recent messages) into every orchestrator prompt. That mechanism is independent of this partial. This partial covers context that lives only in the orchestrator's current conversation or working tree — neither of which the agent inherits automatically.

---

## When to use which block

Decision tree for skill authors choosing what to extract before dispatching an agent:

```
What does the agent need to do?
│
├── Background / facts / web research (researcher role)
│   └─▶ Use CONVERSATION block (below)
│       Reason: the agent needs to know what the user already discussed,
│       decided, or constrained — facts that live only in the chat log.
│
├── Edit code / write a plan tied to the repo (coder, planner roles)
│   └─▶ Use CODEBASE block (below)
│       Reason: the agent needs ground truth from git + files, not assumptions.
│
└── Implementing something the orchestrator just designed
    └─▶ Use BOTH blocks.
        Inject CONVERSATION first (design decisions), then CODEBASE
        (current state of the files the implementation will touch).
```

If the task description in `$ARGUMENTS` is empty or trivial ("ack"), skip extraction and emit a single-line note in the task file: `No orchestrator context — agent should explore ad-hoc.`

---

## CONVERSATION block (canonical)

> Scan the current conversation for anything related to the dispatched task. Include: prior decisions, code snippets, findings, constraints, user preferences, or earlier research that bears on the task.
>
> If relevant context is found:
> - Summarize as **3–8 concise bullet points, max ~600 chars total**
> - Focus on facts and decisions, not narration ("we decided X", "the constraint is Y", "user prefers Z")
> - Omit anything unrelated to the task
>
> If nothing relevant is found, set the variable to an empty string.
>
> Inject into the task file under the heading `## Context from current conversation`. Add the line:
> `Use this context to avoid re-researching known ground and to align findings with existing decisions.`

Default variable name: `CONVERSATION_CONTEXT`.

---

## CODEBASE block (canonical)

> Using the task description, explore the relevant project state. All commands below are read-only — run them in parallel:
>
> ```bash
> # Recent git activity
> git log --oneline -10
>
> # Current changes
> git status --short
>
> # Find files matching key terms from the task description
> # Extract 2-4 keywords from the task description and grep for them
> grep -rn "<keyword1>\|<keyword2>" --include="*.py" --include="*.ts" --include="*.md" . | grep -v venv | grep -v ".git" | head -30
> ```
>
> Then read the **1–3 most relevant files (≤80 lines each)** based on grep hits and git history.
>
> Synthesize into **4–8 concise bullets**: current state, relevant files, recent changes, known constraints. Omit anything unrelated to the task.
>
> If the task description is vague or no relevant files found, set the variable to: `"No specific files identified — agent should explore ad-hoc."`
>
> Inject into the task file under the heading `## Context from codebase`. Add the line:
> `Use this context to avoid re-exploring known ground. Trust it as the state at dispatch time.`

Default variable name: `CONTEXT_SUMMARY`.

---

## Why this is a markdown partial, not Python

Both extraction steps are orchestrator-side prompt instructions — the orchestrator (Claude in the main session) performs the extraction by following these instructions, not by calling a Python helper. The "shared helper" therefore has to live in markdown that the dispatching command can reference. If extraction ever moves into deterministic code, lift it into `src/` and have this partial point at the new module.
