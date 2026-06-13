---
name: runs
description: >-
  Inspect the durable agent I/O ledger and restore the repo to a task's pre-run
  state. Use when the user asks "what did agent/task X do", wants the input or
  output of a previous run/conversation, or says restore|undo|checkout|rewind the
  repo to before task/node N. Wraps `juggle runs ...` (query) and
  `juggle runs restore ...` (per-task VCS restore).
triggers:
  - /juggle:runs
  - what did agent X do
  - what did task X do
  - previous run input/output
  - restore repo to before task
  - undo task / rewind repo / checkout before task
---

# /juggle:runs — Agent I/O ledger + per-task VCS restore

Every agent dispatch is recorded in the append-only `agent_runs` ledger: the full
INPUT prompt, the OUTPUT (handoff/result + diffstat), and — since v1.65.0 — VCS
provenance (repo path, vcs type, before/after SHA, dirty flag). This skill covers
**querying** that ledger and **restoring** the repo to a task's pre-run state.

## When to use

- "What was sent to / what did agent (or task) X do?" → query.
- "Show me the input and output of the last run on this topic." → query.
- "Restore / undo / rewind / check out the repo to before Task N." → restore.

## Query

```bash
juggle runs [--project P] [--topic T] [--task N] [--thread TID] [--limit K] [--json]
juggle runs show <run_id> [--json]      # full INPUT / OUTPUT / DIFFSTAT for one run
juggle runs prune --older-than 90d      # manual retention
```

`juggle runs` lists newest-first with an INPUT→OUTPUT teaser. Filter by any key.
`runs show` prints the complete sent prompt, the output, and the diffstat.
(`--node` is accepted as a deprecated alias for `--task`.)

## Restore

```bash
juggle runs restore --task <id> [--latest]      # or --thread <id>
```

- Targets the **earliest** run for the selector by default; `--latest` picks the
  most recent run.
- Reads that run's `before_sha` / `repo_path` / `vcs_type` and creates a safety
  branch `juggle/pre-<task>-<short_sha>` at the pre-run commit, then switches to
  it (git branch+switch; hg bookmark). The original branch and any later commits
  are left **intact** — this never rewrites history.
- **Refuses** if the working tree is dirty (no stash, no clobber) — commit or
  discard first.
- Graceful no-ops: a run with no VCS provenance, or whose HEAD didn't change
  during the run, prints a clear message and does nothing. A missing/moved
  repo_path is a clean error.

## Notes

- The ledger is written automatically at the dispatch and completion choke
  points — never hand-edit it.
- `git` and `hg` are both supported; non-repo working dirs simply record
  `vcs_type = NULL` and are not restorable.
