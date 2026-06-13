---
description: Inspect the agent I/O ledger (input/output per dispatch) and restore the repo to a task's pre-run VCS state
allowed-tools: Bash, Skill
---

# /juggle:runs — Agent I/O ledger + per-task VCS restore

Query the durable `agent_runs` ledger, or restore the repo to a task's pre-run
commit on a safety branch.

**Syntax:**
```
/juggle:runs                                  # list recent runs (newest-first)
/juggle:runs --topic <T> [--task <N>] [--json]   # filter the ledger
/juggle:runs show <run_id>                    # full INPUT / OUTPUT / DIFFSTAT
/juggle:runs restore --task <id> [--latest]   # checkout repo to before that task
```

Invoke the `juggle:runs` skill, then run the matching `juggle runs ...` command:

- **Query:** `juggle runs [--project|--topic|--task|--thread|--limit|--json]`,
  `juggle runs show <id>`. (`--node` is a deprecated alias for `--task`.)
- **Restore:** `juggle runs restore --task <id> [--latest]` (or `--thread <id>`)
  creates `juggle/pre-<task>-<short_sha>` at the pre-run commit and switches to
  it; refuses on a dirty tree; no-ops when nothing changed. Original branch +
  later commits stay intact.
