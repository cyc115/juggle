---
description: Show all juggle agents with status, pane, thread, and age
allowed-tools: Bash
---

# /juggle:show-agents — Display Agent Pool

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py list-agents
```

Print output verbatim. No reformat.

## Error handling

On "No agents.":
> "No agents running. Use spawn-agent to create one."
