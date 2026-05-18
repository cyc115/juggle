Based on comparing the doc to the source code, I found several stale sentences:

**STALE DRIFT DETECTED**

1. **PostToolUse hook**: The doc says it "Link `[JUGGLE_THREAD:X]` tag → task_id in DB" but the actual handler (juggle_hooks.py:409-502) detects orchestrator violations (foreground Agent calls, search tool usage, context leaks), clears pending decisions after AskUserQuestion, and warns about leaked context blocks — it does NOT link thread tags to task IDs.

2. **Thread status value**: The doc says `complete-agent` sets `threads.status = "done"` but the code (juggle_cmd_agents.py:148) actually sets it to `"closed"`.

3. **Stop hook**: The doc says it "Mark pending notifications as delivered" but the actual handler (juggle_hooks.py:223-266) captures the last assistant message, records Hindsight entries, and detects permission-asking violations — it does NOT mark notifications as delivered.

**Rewrites:**

| Hook | Trigger | Purpose |
|---|---|---|
| `UserPromptSubmit` | Every user message | Inject topic state + pending notifications as `additionalContext` |
| `PostToolUse` | After Agent tool completes | Detect violations (foreground calls, context leaks), clear pending decisions |
| `Stop` | Session end | Capture last assistant message, retain to Hindsight, detect permission-asking |
| `SessionStart` | Resume / context compact | Restore current thread context |

And in the Data Flow, change:
- `threads.status = "done"` → `threads.status = "closed"`