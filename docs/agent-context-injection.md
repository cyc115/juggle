# Agent context injection pattern

How an agent ends up with the right context when it starts. Two independent mechanisms cover two distinct sources of context.

## 1. DB-persisted thread state — automatic

The `UserPromptSubmit` hook calls `src/juggle_context.py`, which reads the current SQLite state (open topics, action items, recent thread messages) and prepends it to every orchestrator prompt. The orchestrator then passes whatever is relevant down to dispatched agents.

This is wired up centrally. No skill needs to opt in.

## 2. Orchestrator-side conversation + working-tree context — per-skill

The DB does **not** capture:

- The user's in-flight conversation that has not yet been committed to a thread message
- The current state of the working tree (uncommitted edits, untracked files, recent local commits)

Both are needed by dispatched agents but only the orchestrator can see them. So the responsibility lives in the **dispatching skill** (`/juggle:deep-research`, `/juggle:delegate`, and any future skill that fires an agent).

The canonical extraction prose lives in [`commands/_context-extraction.md`](../commands/_context-extraction.md). It defines two blocks:

| Block | Source | Used by |
|---|---|---|
| **CONVERSATION** | The current chat session | researcher-style tasks; design decisions just made |
| **CODEBASE** | `git log` / `git status` / `grep` / file reads | coder + planner tasks; anything that touches files |

The partial includes a decision tree showing which block(s) apply to which role.

### How a skill author uses the pattern

1. In the skill's markdown (`commands/<name>.md`), reference the appropriate block by link: `see commands/_context-extraction.md (conversation block)`.
2. Keep the variable names consistent: `CONVERSATION_CONTEXT` for the conversation block, `CONTEXT_SUMMARY` for the codebase block.
3. Inject the variable into the agent's task file under the heading specified in the partial.

Do **not** copy-paste the extraction prose into the skill. The partial is the single source of truth; duplication causes drift.

### Why markdown, not Python

The extraction is performed by Claude (the orchestrator) at dispatch time, following prose instructions. It is not deterministic code — picking which 3–8 bullets matter requires a model in the loop. If extraction ever becomes deterministic, it should move into `src/` and the partial should point at the new module.
