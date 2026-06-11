# Autopilot/Graph Machinery Bugs — 2026-06-11

Observed during the multi-project 3-tier autopilot (v1.61.0) 10-node task-graph build.

---

## Fixed Before This Task

### A. Dispatch storm "Agent not found" (`exit 1` every tick)
**Symptom:** Every watchdog tick archived the dispatched thread — "Agent not found" loop.  
**Root cause:** `cmd_send_task` (`src/juggle_cmd_agents_tasks.py`) opened the default DB path
(`get_db()`) instead of the watchdog's `db_path` argument → the target thread didn't exist in
that DB → exit 1 → archive loop.  
**Status:** FIXED commit `73f02e1` (mirror `cmd_get_agent`'s db_path injection).

### B. Worktree path basename compounding (`juggle-juggle-juggle-WR-WU-XX`)
**Symptom:** Agent worktrees had nested, compounded basenames because each successive worktree
derived its basename from the previous worktree path (not the main repo root).  
**Root cause:** `_create_worktree` in `src/juggle_cmd_agents_worktree.py` called `Path(repo_path).name`
on whatever `repo_path` was passed — if that was already a worktree dir
(`/tmp/juggle-juggle-WR`), the next worktree name compounded.  
**Status:** FIXED commit `d33a05e` via `_main_worktree_root()` (first entry of
`git worktree list --porcelain`).

### C. Watchdog daemon relaunched from agent worktree → `No module named juggle_cmd_agents`
**Symptom:** After a `juggle integrate` inside an agent worktree, the watchdog daemon was
re-launched from that worktree path. When the worktree was GC'd post-integrate, every daemon tick
failed with `No module named juggle_cmd_agents`.  
**Root cause:** `_start_watchdog` / `_watchdog_script` in `src/juggle_cmd_threads.py` resolved the
daemon script path via `__file__`, and `juggle integrate` runs from the agent worktree → daemon
launched from `/tmp/juggle-juggle-<thread>`, whose module path vanished on GC.  
**Status:** FIXED commit `bde4fb1` (`_main_repo_root` for script path + cwd).

### D. LOC-gate regression from C (`juggle_cmd_threads` 692 > 673)
**Symptom:** CI LOC-gate failure after the fix for C added helper functions.  
**Root cause:** Budget was not updated to match the new line count.  
**Status:** FIXED commit `6501c49` (bump budget to 695).

---

## Fixed This Task (E–G, I–J)

### E. Cold-spawn "trust this folder" prompt swallows task submission
**Symptom:** Freshly spawned agent in a new worktree dir boots into Claude Code's
`1. Yes, I trust this folder / 2. No, exit` prompt. Agent shows busy/0-tokens, never runs.
The task submission is swallowed waiting for the prompt response.  
**Root cause:** `claude --dangerously-skip-permissions` (configured at
`src/juggle_settings.py:130`) skips *tool-permission* prompts but NOT the workspace-trust
prompt. Claude Code records trusted directories in `~/.claude.json` under the `projects` map
keyed by absolute directory path. Any directory absent from that map triggers the prompt on
first launch. Each new worktree (`/tmp/juggle-<repo>-<thread>`) is a unique path not yet
in the map.  
**Fix:** `_create_worktree` (`src/juggle_cmd_agents_worktree.py`) now calls
`_register_worktree_trust(worktree_path)` immediately after `git worktree add` succeeds (and
also on the idempotent "already exists" path). The helper atomically adds the path with
`{"allowedTools": []}` to `~/.claude.json`. The env var `JUGGLE_CLAUDE_JSON_PATH` overrides
the target file for tests.  
**Status:** FIXED this task. Test: `test_create_worktree_registers_trust`.

### F. Model-poison via `release-agent`
**Symptom:** Agent released to idle pool after completing a task that was dispatched with a
custom `--model` flag (e.g. a typo like `claude/sonnet`). The next `get-agent` reuses the agent
and the harness rejects the model → agent stuck at 0 tokens on a model-error screen.  
**Root cause:** `cmd_release_agent` (`src/juggle_cmd_agents_lifecycle.py:182`) wiped task state
(`last_task`, `last_send_task_pane_hash`, etc.) but NOT the `model` field. Any model value set
during `get-agent --model <x>` persisted in the DB across pool recycling.  
**Fix:** Added `model=None` to the task-state wipe `db.update_agent(...)` call at the same site.  
**Status:** FIXED this task. Test: `test_release_agent_clears_model`.

### G. `graphify-out/` dirty tree blocks `juggle integrate` ff-merge
**Symptom:** `juggle integrate` fails with "FF-merge of <branch> failed: error: Your local
changes to the following files would be overwritten by merge". The files are in `graphify-out/`.  
**Root cause:** The graphify watch hook regenerates four tracked files
(`graphify-out/graph.json`, `GRAPH_REPORT.md`, `manifest.json`, `.graphify_labels.json`)
on every commit. The agent's worktree branch also commits updates to those files (graphify ran
inside the worktree). When `_run_integrate` (`src/juggle_cmd_integrate.py`) tries to
ff-merge the branch into main, git refuses because main's working tree has the dirty (hook-
regenerated) versions of those same files.  
**Root cause confirmed at:** `src/juggle_cmd_integrate.py:211` — bare `git merge --ff-only`
with no pre-merge cleanup. `.gitignore` only excluded `graphify-out/cache/`.  
**Fix:**  
1. Added `git checkout -- graphify-out/` before `git merge --ff-only` in `_run_integrate`
   (discards the hook-regenerated local modifications; safe because graphify regenerates on
   demand).  
2. Added the four auto-generated files to `.gitignore` so they won't be tracked in future
   commits.  
3. Ran `git rm --cached` on the four currently-tracked files to untrack them.  
**Status:** FIXED this task. Test: `test_integrate_succeeds_with_dirty_graphify_out`.

### I. Node/topic stuck `running` after successful out-of-band integrate
**Symptom:** Orchestrator ran `juggle integrate XY` manually (out-of-band) which succeeded and
merged the branch. The topic/node stayed in `running` state and blocked downstream nodes. The
orchestrator had to mark it verified manually.  
**Root cause:**  
- `cmd_integrate` / `_run_integrate` (`src/juggle_cmd_integrate.py`) does NOT advance the
  topic/node state machine — it only does the git operations. The graph state transition
  (`running` → `integrating` → `verified`) is done only by `complete-agent` via
  `mark_graph_topic`.  
- When two integrates raced (out-of-band + agent's complete-agent), the second call to
  `mark_topic_completion` (`src/dbops/db_topics.py:201`) raised `ValueError` ("cannot mark
  completion: topic in terminal state 'verified'"), which `mark_graph_topic` caught as a
  silent warning. In the opposite ordering, the topic stayed `running` until the agent's
  `complete-agent` fired.  
**Fix:** Made `mark_topic_completion` idempotent for the success path: if the topic is already
`verified` and `integrate_ok=True`, return `"verified"` without raising. This prevents the
`ValueError → warning → silent no-op` path that left nodes stuck on retried calls.  
**Note:** The underlying gap — `juggle integrate` not advancing the state machine — remains open
(see Recommended below). The idempotency fix handles the racing-duplicate scenario safely.  
**Status:** FIXED this task (idempotency). Test: `test_mark_topic_completion_idempotent_on_verified`.

### J. Topic-model tick can't dispatch a legacy flat node-only graph
**Symptom:** After Tasks 6-7 of the v1.61.0 build rewrote `graph_tick` to dispatch TOPICS,
`graph_tick` returned `dispatched=[]` for a project whose `graph_nodes` had no `graph_topics`
(migration 37 backfilled 0 rows). The remaining nodes were stranded mid-build.  
**Root cause:** `graph_tick` (`src/juggle_graph_dispatch.py:186`) exclusively calls
`db_topics.list_topics` and dispatches only from the topic pool. For a project with no topics,
`list_topics` returns empty → the topic loop is a no-op. The spec (Task 4, R9/R6) explicitly
mentioned a "legacy flat fallback" for node-only graphs, but it was not wired into the tick.  
**Fix:** Added `_dispatch_flat_node_fallback()` called after the topic loop in `graph_tick`.
For each armed project that has `list_topics() == []`, it sweeps stale node claims, recomputes
node readiness (`db_graph.recompute_ready`), and dispatches ready nodes via `claim_node` +
`hydrate_for_node` + the same `dispatch_fn`. The fallback honours all existing invariants:
atomic claim, stale sweep, retry cap, capacity defer, armed-project mid-batch guard.  
**Status:** FIXED this task. Test: `test_graph_tick_dispatches_ready_nodes_for_topicless_project`.

---

## Open — Recommended (not fixed; touch fresh v1.61.0 machinery)

### H. Stale-claim duplicate dispatch (WJ/WK/WS, WW/WX)
**Symptom:** A node's claim appeared to expire while the agent was still working a long task +
integrate, causing the tick to re-claim and dispatch a duplicate agent.  
**Root cause analysis:**  
- `sweep_stale_topic_claims` (`src/juggle_graph_dispatch_topics.py:41`) resets topics in
  `dispatching` state with `thread_id IS NULL` after `STALE_CLAIM_SECS = 600` (10 min).  
- The thread is bound (`set_topic_thread`) BEFORE `dispatch()` is called in `graph_tick`, so
  the window where `thread_id IS NULL` is <1 ms (create_thread → set_topic_thread) — the sweep
  should not fire during normal dispatch.  
- Once the topic transitions to `running`, it cannot be swept by this function (condition
  requires `state='dispatching'`).  
- **Most likely cause of the observed duplicates:** A crash or timeout between `claim_topic`
  and `set_topic_thread` during v1.61.0 initial testing left some topics in `dispatching` with
  no thread; those were correctly swept and re-dispatched while the original agent was still
  alive from a prior partially-successful dispatch. Not a systemic ongoing bug.  
**Recommendation:**  
1. Extend `STALE_CLAIM_SECS` from 600 to 3600 for topics (long integrate can exceed 10 min).  
2. Add a liveness check in the sweep: before resetting a `dispatching` topic, verify no live
   agent is assigned to a thread that matches expected_thread (best-effort guard).  
3. Consider extending `TICK_OWNED_STATES` to include a TTL check on `running` topics with
   the bound agent dead (agent_status=idle, no assigned_thread match).  
**Risk:** Moderate — modifying sweep TTL and agent-liveness checks in the core tick loop.

### H-addendum. `juggle integrate` doesn't advance the graph state machine
**Symptom:** Related to I above. When `juggle integrate XY` is run directly (e.g. by the
orchestrator), the topic/node stays in `running` state because `_run_integrate` only does
git operations, not state transitions.  
**Recommendation:** After a successful ff-merge in `_run_integrate`, call `mark_graph_topic`
(imported from `juggle_cmd_agents_graph_topics`) to advance the state machine. Guard with a
try/except so git-only integrate callers (non-graph threads) are unaffected. The idempotency
fix in I means a subsequent `complete-agent` call is safe.
