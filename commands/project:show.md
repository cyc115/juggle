---
name: project:show
description: Show full details for a Juggle project (name, objective, threads)
allowed-tools: Bash
---

# /juggle:project:show — Show a Juggle Project

Show full details for a project. Usage: `/juggle:project:show <project_id>`

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project show $ARGUMENTS
```

Report: name, objective, out-of-scope, assigned threads (label + status).
