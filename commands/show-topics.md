---
description: Show all open conversation topics with status
allowed-tools: Bash, Agent
---

# /juggle:show-topics — Display Open Topics

When the user runs `/juggle:show-topics`:

## Step 1: Sweep stale summaries

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-stale-threads
```

For each thread listed as stale, spawn a Haiku background summarizer agent using the
template from `commands/start.md § Auto-Summary`. Wait for all to complete before
rendering — they are fast (single DB read, ~100 token output).

If no stale threads, proceed directly to Step 2.

## Step 2: Render topics

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py show-topics
```

Print the output **verbatim** — do not reformat or summarize it.

## Output format

Each thread is shown with:
- **Header**: `{branch} {emoji} **[{id}] {topic}**  ({last_active})  {state_suffix}`
- **Summary**: 1–2 sentences from the thread's summary field
- **Key decisions**: each prefixed `✅` (skipped if none)
- **Open questions**: each prefixed `❓` (skipped if none)
- **Last 2 exchanges**: labeled `Last:` and `Prior:`, showing Q and A
- For **waiting** threads (`⏸️`): the full pending question is shown (no truncation)
- For **background** threads (`🏃`): an agent status line prefixed `⏳`

### State emoji legend

| Emoji | State | Condition |
|-------|-------|-----------|
| 👉 | Current | This is the active thread |
| 🏃‍♂️ | Agent running | `status == "background"` |
| ⏸️ | Waiting for you | Last assistant message ends with `?` |
| 💤 | Idle | Last assistant message has no `?` AND inactive > 30 min |
| ✅ | Done | `status == "done"` |
| ❌ | Failed | `status == "failed"` |
| 🗄️ | Archived | Inactive > 48 hours |

## Error handling

If the command errors or outputs "No topics." / juggle not active:
> "Juggle mode isn't active. Run /juggle:start to activate it."
