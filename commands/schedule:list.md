---
name: schedule:list
description: List everything Juggle schedules — OS schedules (scripts on a timer) and loops (recurring agent-work topics), each row type-tagged
allowed-tools: Bash
---

# /juggle:schedule:list — List scheduled tasks & loops

Shows every recurring thing Juggle knows about, each row tagged by TYPE:
- **OS** — a script/binary on a launchd/systemd/cron timer
- **LOOP** — a recurring agent-work topic Juggle drives to completion each cadence

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py schedule list
```

Delete any row with `/juggle:schedule:delete <id>`.
