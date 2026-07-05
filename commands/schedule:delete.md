---
name: schedule:delete
description: Delete a scheduled task or loop by id — soft by default (a loop pauses + its project closes, recoverable); --purge hard-removes a loop
allowed-tools: Bash
---

# /juggle:schedule:delete — Delete a schedule or loop

Usage: `/juggle:schedule:delete <id> [--purge]`

Type-dispatched by id (run `/juggle:schedule:list` to see valid ids):
- **OS schedule** → uninstalled from launchd/systemd/cron.
- **LOOP** → **soft by default**: the loop is paused and its project closed
  (recoverable via `/juggle:project:open` + resume). `--purge` hard-removes the
  loop row (non-recoverable).
- **Unknown id** → fails loud (never a silent no-op).

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py schedule delete $ARGUMENTS
```
