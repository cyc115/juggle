---
description: Show all open conversation topics with status
allowed-tools: Bash, Agent
---

# /juggle:show-topics — Display Open Topics

## Step 1: Sweep stale summaries

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-stale-threads
```

For each stale thread: spawn Haiku background summarizer (see `commands/start.md § Auto-Summary`). Wait for all to complete before rendering.

No stale threads → go to Step 2.

## Step 2: Render topics

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py show-topics
```

Print output verbatim. No reformat.

## Output format

Each thread shows:
- **Header**: `{branch} {emoji} **[{id}] {topic}**  ({last_active})  {state_suffix}`
- **Summary**: 1–2 sentences
- **Key decisions**: prefixed `✅` (skip if none)
- **Open questions**: prefixed `❓` (skip if none)
- **Last 2 exchanges**: `Last:` and `Prior:` with Q and A
- **Waiting** (`⏸️`): full pending question, no truncation
- **Background** (`🏃`): agent status prefixed `⏳`

### State emoji legend

| Emoji | State | Condition |
|-------|-------|-----------|
| 👉 | Current | Active thread |
| 🏃‍♂️ | Agent running | `status == "background"` |
| ⏸️ | Waiting | Last assistant message ends with `?` |
| 💤 | Idle | No `?` AND inactive > 30 min |
| ✅ | Done | `status == "done"` |
| ❌ | Failed | `status == "failed"` |
| 🗄️ | Archived | Inactive > 48 hours |

## Error handling

On error or "No topics.":
> "Juggle not active. Run /juggle:start."
