---
name: project:add-task
description: Inject a single new task (node) into an existing project task-graph mid-execution, without disarming or restarting autopilot. Brainstorm the task with the user, derive its id/deps/verify_cmd/prompt, confirm, then upsert it via `juggle graph add-node` — the watchdog tick claims it when its deps verify.
allowed-tools: Bash
---

# /juggle:project:add-task <project> [--auto-approve]

Add ONE new task to an already-loaded, possibly-executing project task-graph.
The graph keeps running: existing nodes are untouched, the new node lands as
`pending` and the watchdog tick claims it once its dependencies verify. This is
the conversational front-end; the validated, atomic, guarded upsert is done by
the CLI (`juggle graph add-node`) — same code-vs-prompt split as
`/juggle:toggle-autopilot` (markdown) → `juggle autopilot` (CLI).

The CLI is the source of truth for every safety rule — it refuses (nonzero
exit, graph byte-unchanged) on unknown deps, a cycle, an empty prompt, a
verify_cmd lint failure, or touching a PROTECTED node. Your job is to flesh out
a good task and call it; never hand-write graph state.

## Conversational flow

1. **Brainstorm** the task with the user in the main thread. What is it, why
   now, where does it sit in the existing graph? Run a brief devil's-advocate
   pass (is this really a new node, or an edit to an existing one? does it
   belong before or after an existing node?). Resolve open questions before
   proceeding.

2. **Derive** the node fields, reading the live graph first
   (`juggle autopilot status <project>` and/or `juggle project-graph` state):
   - **id** — a stable, unique node id (kebab/snake, e.g. `rate-limit`). If it
     collides with an existing node, that node must be in a mutable state to be
     re-added (see guard); otherwise pick a new id.
   - **title** — one short line.
   - **deps** — EXISTING node ids this task follows (upstream). Depending on an
     already-`verified` node is fine and makes the new node immediately
     eligible; no deps ⇒ it is `ready` at once. Upstream deps may be in ANY
     state.
   - **required-by** (optional) — EXISTING node ids that must now wait on this
     task (downstream insert, e.g. insert a task *before* an `e2e` node). Each
     such target must be in a MUTABLE state (`pending | ready | failed-* |
     blocked-failed`) — a `running`/`integrating`/`verified`/`dispatching`
     target is REFUSED by the CLI.
   - **verify_cmd** (optional) — a machine-checkable predicate; runs through the
     SAME lint as graph load (allowlisted executables only, no shell operators).
   - **prompt** — the dispatch prompt for the coder agent. Long prompts can be
     piped on stdin (`--prompt -`).

3. **Node card + confirmation.** Show a card with id, title, deps, required-by,
   verify_cmd, and the prompt, then wait for an explicit user reply. ONLY skip
   this gate when the user passed `--auto-approve`.

4. **Add it** via the CLI:

   ```bash
   # short prompt inline:
   juggle graph add-node --project <project> --id <node-id> --title "<Title>" \
     --prompt "<dispatch prompt>" \
     --deps a,b --required-by e2e --verify-cmd "pytest tests/test_x.py -q"

   # long prompt via stdin:
   cat <<'PROMPT' | juggle graph add-node --project <project> --id <node-id> \
     --title "<Title>" --deps a,b --prompt -
   <multi-line dispatch prompt>
   PROMPT
   ```

   On a validation/guard error the graph is unchanged — fix the offending field
   (rename a colliding id, drop a cyclic edge, fill the prompt, pick a different
   required-by target) and retry. Report the resulting state and any downstream
   nodes whose state changed (a `required-by` insert can demote a previously
   `ready` node back to `pending`).

## Tick-owned carve-out (do NOT manually dispatch the new node)

The new node is part of the armed project's graph, so it is **tick-owned**: the
watchdog claims it, dispatches a hydrated coder agent, integrates, verifies, and
marks it — exactly like every other graph node. Do **not** `send-task` to it and
do **not** otherwise dispatch it yourself. After `add-node` succeeds, your job is
to report the resulting state and let the tick run. Triage only failures
(`failed-*`/`blocked-failed` are operator territory).
