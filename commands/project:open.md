---
name: project:open
description: Restore a closed Juggle project — project and all its topics reappear in the cockpit
allowed-tools: Bash
---

# /juggle:project:open — Restore a Closed Project

Usage: `/juggle:project:open <project_id|name>`

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project open $ARGUMENTS
```

Restores a closed project: sets status back to active and makes all its threads visible in the cockpit again.
