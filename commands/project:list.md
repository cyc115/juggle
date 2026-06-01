---
name: project:list
description: List all Juggle projects with ID, name, objective, and thread count
allowed-tools: Bash
---

# /juggle:project:list — List Juggle Projects

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project list
```

Report: project ID, name, objective (1 line), thread count. Table format.
