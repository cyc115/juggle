# Self-Heal Diagnosis Agent Prompts

Fill in `<…>` tokens from the relevant `error_events` row (use `juggle list-selfheal` to retrieve).

---

## Class A — Juggle Python Exception

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class A (Juggle Python exception)

error_event_id: <id>
signature:      <signature_hash>
exc_type:       <exc_type>
entrypoint:     <entrypoint>
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Traceback
<full traceback text from error_events.traceback>

### Task

You are a researcher. Diagnose this exception and propose a minimal code patch.

1. Read the source file(s) named in the traceback using semble MCP or Read tool.
2. Identify the root cause (missing guard, wrong assumption, off-by-one, etc.).
3. Produce a minimal unified diff of the fix (no refactoring, no style changes).
4. Assess confidence: HIGH / MEDIUM / LOW. Note any assumptions.

### Output format (for the action item message)

ROOT CAUSE: <one sentence>
FIX (unified diff):
--- a/src/<file>
+++ b/src/<file>
@@ ... @@
 <context>
-<old>
+<new>
CONFIDENCE: HIGH|MEDIUM|LOW
CAVEATS: <if any>

### Completion

After diagnosis:
1. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <thread_id> \
     "Self-heal A: <exc_type> in <entrypoint> — <one-line root cause>" \
     --type decision --priority high
2. Note the returned action_item_id.
3. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> \
     awaiting_approval --action-item-id <action_item_id>
4. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> \
     "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." \
     --retain "Self-heal A sig=<sig8>: <root cause in 10 words>"

NEVER auto-apply the patch. The user must approve the action item first.
```

---

## Class B — Orchestration Tool Error

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class B (Orchestration tool error)

error_event_id: <id>
signature:      <signature_hash>
tool:           <entrypoint>   (the tool that errored)
juggle_ref:     <juggle_ref>   (the Juggle path that triggered it)
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Tool error
<traceback / error_text from error_events.traceback>

### Tool input that caused the error
<command_args JSON from error_events.command_args>

### Task

You are a researcher. Diagnose why Juggle's instructions caused this tool error.

Decision tree:
- If a defensible code surface exists (e.g., a preflight check, a schema-load guard
  before arming the tool): propose a code guard (minimal diff to the relevant .py file).
- If no defensible code surface exists (e.g., the fix is purely how instructions are worded):
  propose an instruction patch to the culprit command/skill markdown at <juggle_ref>.

Steps:
1. Read <juggle_ref> (the command/skill markdown) using Read tool.
2. Read the relevant source file if a code guard is feasible (use semble MCP).
3. Identify exactly which instruction led the orchestrator to call <tool> incorrectly.
4. Produce the minimal fix:
   - Code guard: unified diff.
   - Instruction patch: exact replacement lines for the culprit section of the markdown.

### Output format

ROOT CAUSE: <one sentence — which instruction / missing guard>
FIX TYPE: code_guard | instruction_patch
FIX:
<unified diff OR markdown diff with --- / +++ lines>
CONFIDENCE: HIGH|MEDIUM|LOW
CAVEATS: <if any>

### Completion

After diagnosis:
1. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <thread_id> \
     "Self-heal B: <tool> error via <juggle_ref_basename> — <one-line root cause>" \
     --type decision --priority high
2. Note the returned action_item_id.
3. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> \
     awaiting_approval --action-item-id <action_item_id>
4. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <thread_id> \
     "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." \
     --retain "Self-heal B sig=<sig8>: <root cause in 10 words>"

NEVER auto-apply the patch.
```

---

## Orchestrator Reaction to Monitor Lines

When you see `[SELFHEAL-A]` or `[SELFHEAL-B]` from `juggle-selfheal-monitor`:

1. `uv run juggle_cli.py list-selfheal` — get `error_event_id` and full details.
2. If the row is stuck in `diagnosing`: `uv run juggle_cli.py selfheal-reset-diagnosing <id>`.
3. Cap check: if another row is `diagnosing`, note "queued" inline; do not dispatch.
4. Dispatch a researcher agent using the Class A or Class B prompt above.
   Fill in `<id>`, `<signature_hash>`, `<entrypoint>`, etc. from `list-selfheal` output.
   Use the current active thread for `<thread_id>`.
