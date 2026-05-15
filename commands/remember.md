---
description: Explicitly retain a memory in Hindsight
allowed-tools: Bash
---

# /juggle:remember — Explicit Memory Retain

Store something in Juggle's long-term memory.

**Usage:** `/juggle:remember <thing to remember>`

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py retain <current_thread_id> "<ARGUMENTS>" --context preferences
```

If no arguments provided, ask: "What should I remember?"

Confirm: `Remembered: "<thing>"`
