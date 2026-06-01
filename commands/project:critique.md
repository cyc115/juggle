---
name: project:critique
description: Re-run the LLM coach on an existing project to sharpen its objective and scope
allowed-tools: Bash
---

# /juggle:project:critique — Critique a Juggle Project

Re-run the LLM coach on an existing project to sharpen its objective and scope. Usage: `/juggle:project:critique <project_id>`

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project critique $ARGUMENTS
```

Report the coach's findings and any suggested changes to name, objective, or out-of-scope.
