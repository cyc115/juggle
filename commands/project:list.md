---
name: project:list
description: List all Juggle projects (including closed) with ID, name, status, last-updated, thread count, and summary
allowed-tools: Bash
---

# /juggle:project:list — List All Juggle Projects

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project list
```

Shows ALL projects including closed. Columns: ID, name, status, last-updated, thread count, 1-line summary. Closed projects shown dimmed.
