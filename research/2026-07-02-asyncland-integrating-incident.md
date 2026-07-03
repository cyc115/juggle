# Incident: async-land stuck `integrating` after successful watchdog integrate (2026-07-02)

## Symptom
Topic `async-land` (project P2, thread CC, 1.97.0 land-poller feature) was
stuck in state `integrating` with `merged_sha` NULL, even though its integrate
demonstrably completed: its commits (1afa1a5, 4561948, 5f4a0b4) were the
current `origin/main` tip, `cyc_CC` was deleted, and the worktree was removed.
`graph reconcile P2` did not repair it (`integrating` is protected/in-flight).
A HIGH action item fired: "completed but UNMERGED — all tasks verified, no
merged_sha".

## RCA
`_run_integrate` (`juggle_cmd_integrate.py`) runs, in order, per successful
`push_mode="direct"` landing:
1. `submit()` ff-merges + pushes → returns `landed_rev`.
2. `_record_merged_sha(db, thread_uuid, main_repo_path, landed_rev)` —
   **fail-soft**: resolves the SHA, then gates the write on
   `is_ancestor(sha, canonical_main_ref(repo))`. Any failure of that ancestry
   check (canonical unresolvable, or a transient miss right after the push)
   silently skips the write — by design, "never blocks integrate".
3. `remove_workspace(...)` — deletes the worktree **and the git branch itself**
   (`git branch -d <branch>`).
4. `db.update_thread(..., worktree_path="", worktree_branch="", main_repo_path="")`
   — clears the thread's worktree bookkeeping.

`cmd_complete_agent` then calls `mark_graph_topic` → `mark_topic_completion`,
which hits `UnmergedVerifyRefused` (no `merged_sha`) and attempts the
2026-07-01 self-heal (`reconcile_topic_state` → `_heal_merged_sha`). That
self-heal resolves the merged commit by **re-reading the branch tip by name**
(`thread.worktree_branch` + `thread.main_repo_path`) — but by the time it
runs, step 3 already deleted the branch ref and step 4 already cleared both
thread fields, in the SAME completion call. The self-heal added specifically
for "integrate succeeded but merged_sha wasn't recorded" is structurally
incapable of firing for the one case it was built for: a *normal, fully
torn-down* successful integrate. Every subsequent `graph_tick`/reconcile hits
the identical dead end forever — the branch is gone, not just the field.

Ruled out: (a) landing-mode/event misrouting — `mark_graph_topic` never passes
`submitted_rev`, so the topic always takes the synchronous `integrate_ok`
path, never `integrate_submitted`; the 1.97.0 land-poller code is inert here.
(b) watchdog-restart race — `_restart_juggle_daemons` (self-repo step 10)
only restarts talkback and kills a *stale* monitor pid; it never signals the
process executing `_run_integrate`/`mark_graph_topic` itself. (c) confirmed:
`UnmergedVerifyRefused` is caught and "self-healed", but the self-heal is a
no-op once teardown has run — this is the actual defect.

## Fix
- `_record_merged_sha` now stashes the resolved-but-unproven SHA (+ the repo
  it resolved in) onto new columns `nodes.pending_merged_sha` /
  `pending_merged_repo` (Migration 61, additive) whenever the ancestry check
  fails or canonical main can't be resolved — a durable, topic-scoped
  breadcrumb independent of the thread/branch that teardown deletes.
- `_heal_merged_sha` (`db_topics_reconcile.py`) now falls back to re-checking
  the pending SHA's ancestry (against CURRENT canonical main, in the pending
  repo) when the branch-name lookup has nothing to work with, and promotes it
  to `merged_sha` on success — still gated by a real `is_ancestor` check
  (never a bypass).
- `graph_guards._resolve_topic_repo` also falls back to `pending_merged_repo`
  (after `thread.main_repo_path`, before the juggle-self-repo default) so the
  post-heal `topic_is_merged` re-check resolves the correct repo even for a
  non-self-repo topic whose thread fields are already cleared.

## Prod state repair
Applied via the fixed code path — `mark_graph_topic`'s existing self-heal call
(no manual DB edit): after this fix landed, project P2's topic `async-land`
was re-run through `reconcile_topic_state`, which promoted its stashed
pending SHA to `merged_sha` and completed →`verified`.

## Regression pins (`tests/test_verified_merged_sha.py`)
- `test_record_merged_sha_stashes_pending_when_canonical_unresolvable`
- `test_heal_merged_sha_promotes_pending_when_branch_already_gone`
- `test_heal_merged_sha_does_not_promote_pending_not_on_main`
- `test_async_land_watchdog_owned_integrate_self_heals_end_to_end` — drives a
  real `_run_integrate` → `mark_graph_topic` completion through the exact
  failure mode; RED on pre-fix code (confirmed by reverting the fix and
  re-running), GREEN after.
- Existing pin `test_reconcile_does_not_verify_null_merged_sha_even_orphaned`
  (unmerged topics never fail-open to verified) remains green, unmodified.
