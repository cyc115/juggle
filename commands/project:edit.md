---
name: project:edit
description: Edit a Juggle project's name, objective, or out-of-scope definition
allowed-tools: Bash
---

# /juggle:project:edit — Edit a Juggle Project

Edit a project's name, objective, or out-of-scope definition. Usage: `/juggle:project:edit <project_id> [--name "..."] [--objective "..."] [--out-of-scope "..."]`

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project edit $ARGUMENTS
```

After editing, show the updated project:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project show <project_id>
```
