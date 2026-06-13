# VCS provenance + per-task restore — design spec

**Status:** Implemented 2026-06-13 (v1.65.0). Topic `T-vcs-checkpoint`, task `vcs-checkpoint`.

## Problem

The `agent_runs` ledger pairs each dispatch's INPUT with its OUTPUT but records
no VCS provenance. The orchestrator cannot answer "check out the commit *before*
Task 5" — it has no record of where the repo's HEAD was when a task was
dispatched, nor whether the worktree was dirty.

## Goal

Capture VCS provenance (repo path, vcs type, before/after SHA, dirty flag) on every
ledgered dispatch, and expose a safe per-task **restore** that recreates the
pre-run state on a fresh safety branch without clobbering the working tree.

## Schema (Migration 40)

Add 5 columns to `agent_runs` via an idempotent ALTER chain. Migration 39 is the
node→task rename (`dbops.migrations_graph`); this is the next free number. Each
`ADD COLUMN` is guarded by a `PRAGMA table_info` existence check so the migration
converges on BOTH a fresh dev DB and the prod DB, where the 5 columns already
exist (they were applied out-of-band during the shared-DB incident).

| column      | type    | meaning                                     |
|-------------|---------|---------------------------------------------|
| `repo_path` | TEXT    | absolute path of the repo at dispatch       |
| `vcs_type`  | TEXT    | `'git'` \| `'hg'` \| NULL (not a repo)      |
| `before_sha`| TEXT    | HEAD sha at dispatch                         |
| `after_sha` | TEXT    | HEAD sha at completion (close_run)           |
| `was_dirty` | INTEGER | 1 if worktree had uncommitted changes        |

`CREATE_AGENT_RUNS` in `dbops/schema_runs.py` gains the same columns so fresh DBs
match.

## VCS abstraction — `src/vcs.py`

A `VCS` Protocol with `head` / `is_dirty` / `make_safety_branch`, two concrete
backends, plus module-level `detect()` / `get_backend()`. All methods are
best-effort and swallow tool errors (return `None`/`False`) — they never raise.

```
detect(path) -> 'git' | 'hg' | None
head(path)   -> sha | None
is_dirty(path) -> bool
make_safety_branch(path, sha, name) -> bool   # create branch/bookmark @ sha + switch
```

- **GitVCS:** `rev-parse HEAD`, `status --porcelain`, `branch <name> <sha>` + `switch <name>`.
- **HgVCS:** `hg id -i`, `hg status`, `hg update -r <sha>` + `hg bookmark <name>`.
- `detect()` dispatches on `.git` / `.hg` (or `git rev-parse --is-inside-work-tree`
  / `hg root`); `get_backend(vcs_type)` returns the matching backend instance.

300-line gate: `src/vcs.py` stays a single focused module (~100 lines).

## Capture (reuse existing ledger choke points)

Best-effort `try/except`, NEVER breaks dispatch or completion.

- **Dispatch** (`cmd_send_task`, where `insert_agent_run` runs): resolve
  `repo_path` from the agent (`agent.repo_path`, fallback thread `worktree_path`);
  `detect()` → `vcs_type`; `before_sha = head()`; `was_dirty = is_dirty()`.
  Passed into an extended `insert_agent_run(...)` signature (new kwargs default
  None so existing callers/tests are unaffected).
- **Completion** (`close_run`): the row being closed already carries
  `repo_path`/`vcs_type`; compute `after_sha = head(repo_path)` and persist it in
  the same UPDATE. This single seam covers `cmd_complete` AND every `mark_graph_*`
  path because they all funnel through `close_run`.

## Restore CLI — `juggle runs restore`

`juggle runs restore --task <id> [--latest]` (`--node` deprecated alias).

1. Pick the target run: EARLIEST run for the task by default; `--latest` → most
   recent. (`--thread` also accepted as an alternate selector.)
2. Read `repo_path`, `vcs_type`, `before_sha`, `was_dirty`, `after_sha`.
3. Edge cases (graceful, exit 0 unless noted):
   - `vcs_type` NULL or `before_sha` missing → "nothing to restore".
   - `repo_path` missing/moved/not a repo → clean error (exit 1).
   - working tree dirty (re-checked live via `is_dirty`) → **REFUSE** with a
     clear message; no stash, no clobber (exit 1).
   - `after_sha == before_sha` → no-op report (nothing changed during the run).
4. Otherwise `make_safety_branch(repo_path, before_sha,
   'juggle/pre-<task>-<short_sha>')` and switch to it (hg: bookmark), leaving the
   original branch and any later commits intact.

## Discoverability (part of DONE)

- `skills/runs/SKILL.md` (id `juggle:runs`) — covers BOTH the query feature
  (`juggle runs ...`, `runs show`) and `juggle runs restore`.
- `commands/runs.md` — `/juggle:runs` slash command.

## Tests

- `GitVCS` + `HgVCS` against tmp repos (detect/head/is_dirty/make_safety_branch).
- Capture at dispatch (before_sha/vcs_type/was_dirty) and completion (after_sha).
- Restore: happy-path, dirty-refuse, non-repo error, no-op, nothing-to-restore,
  selector-required, --node alias.
- Migration 40 idempotency (fresh DB, re-run no-op, pre-existing columns).
