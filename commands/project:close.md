---
name: project:close
description: Close a Juggle project — summarizes it, hides it from cockpit, and stores it for later restore
allowed-tools: Bash
---

# /juggle:project:close — Close a Project

Usage: `/juggle:project:close <project_id|name>`

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project close $ARGUMENTS
```

Summarizes all topics via Claude Sonnet, stores the summary, hides the project and its threads from the cockpit. Restore with `/juggle:project:open`.
