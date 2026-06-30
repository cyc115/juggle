# Incident: Autopilot machinery defects → shared-DB schema corruption

**Date:** 2026-06-13
**Severity:** HIGH — unmerged WIP migrations mutated the live operational DB (`~/.claude/juggle/juggle.db`).
**Status:** Contained (Juggle stopped). Recovery pending.
**Trigger:** Arming P2 ("Juggle Claude Code Plugin") and filing three sequenced features (ledger → rename node→task → vcs-checkpoint) as graph topics.

## Summary

The ledger feature (`T-agent-runs-ledger`, commit `957aed5`, v1.62.0) landed cleanly and merged. The follow-on rename and vcs-checkpoint topics then exposed a chain of autopilot graph-machinery defects that ended with **two unmerged WIP features' schema migrations being applied to the shared production DB**, and a feature being marked `verified` while sitting unmerged on a `cyc_*` branch.

## Defects (root causes)

| # | Defect | Root cause | Evidence |
|---|--------|-----------|----------|
| 1 | **Empty-topic dispatch race (TOCTOU)** | `T-vcs-checkpoint` topic was created (via low-level `create_topic`) 17.6s before its blocking node/edge was added. An empty topic has no member nodes → no derived cross-topic deps → looked dependency-free → tick claimed + dispatched it. | topic `created_at` 21:03:15.2 vs node `created_at` 21:03:32.8; action item 4527 "submission not verified for pane %2941" |
| 2 | **`reconcile` demotes a running topic without cleaning its live agent** | `reconcile_project_topics` reset `T-rename`/`T-vcs` `running→pending` but left their dispatched tmux agents alive + bound → orphans + double-dispatch risk. | post-reconcile: topics `pending`, agents still `busy` on their threads |
| 3 | **`decommission-agent` marks the bound topic `verified`** | Decommissioning the agent on `T-vcs-checkpoint` flipped the topic to `verified` though nothing merged. | `decommission-agent` → topic state `verified` |
| 4 | **`verified` does not require a merge to main** | Completion/verify path marks `verified` on agent completion, not on a merge check. Unmerged WIP (`cyc_XU`) was marked done. | `vcs-checkpoint` task `verified`, work only on `cyc_XU` `ddeb6ab`, never merged |
| 5 | **Agent migrations run against the shared production DB** *(most severe)* | Worktree agents run schema migrations against `~/.claude/juggle/juggle.db` (shared), not an isolated DB. Two unmerged features' migrations applied to prod. | prod DB now has `graph_tasks` (was `graph_nodes`), `agent_runs.task_id` (was `node_id`) + 5 VCS columns — none merged to main |
| 6 | **Cockpit graph panel shows raw UUID prefix, not the human label** | The graph-panel node's `user_label` is not populated from its bound thread, so `juggle_cockpit_graph_panel.py:84` falls back to `thread_id[:4]`. Compounded by `juggle_cockpit_model.py:219` still selecting from `graph_nodes` (renamed → `graph_tasks` in prod), which graceful-degrades to empty state. | task 13 rendered `[dc2e]` instead of `[XW]` (thread `dc2e7617…` has `user_label='XW'`) |

**Contributing factors:**
- No atomic "create topic + first node" path — `juggle graph add-task` requires a pre-existing topic, forcing the racy two-step (feeds #1).
- Parallel out-of-order dispatch — vcs-checkpoint (depends on rename) was built *before* rename despite the edge (the race let both run in parallel).
- Cross-cutting theme: **topic state, node state, agent reality, and merge state are allowed to diverge.** A single invariant — *a topic/node is `verified` iff its commit is an ancestor of `main`, and is only `running` iff a live agent is bound* — would prevent #2/#3/#4.

## Blast radius
- Production DB schema mutated (`graph_nodes→graph_tasks`, `agent_runs.node_id→task_id`, +VCS cols). Deployed `main` (957aed5) expects the old names → graph/node-table code paths break (topic-only paths like `reconcile` still work).
- No data lost: 13 task rows intact; worktrees + branches persist (`cyc_XU ddeb6ab` = the complete, clean VCS feature; `cyc_XQ` = WIP rename on a pre-ledger base).

## Recovery plan
1. **Restore prod DB to known-good for deployed main:** rename `graph_tasks → graph_nodes`, `agent_runs.task_id → node_id`, drop the unmerged VCS columns. Reset corrupted task/topic states (`vcs-checkpoint`, `rename-node-to-task`) to a consistent baseline. Clean orphaned agents + stale worktrees.
2. **Preserve good work:** keep `cyc_XU ddeb6ab` (the complete VCS feature) to re-land as a *properly merged* change later.
3. **Re-do the rename** as a clean, merged change — with migrations applied only on merge, never from a worktree against shared prod.

## Durable fixes (code guards — not prompt-only)
- **G1 (verified⟺merged):** completion/verify path refuses to mark `verified` unless the task's commit is an ancestor of `main` (`git merge-base --is-ancestor`).
- **G2 (no shared-DB migration from agents):** the migration runner refuses to mutate `~/.claude/juggle/juggle.db` when invoked from a worktree/agent context; agents get an isolated DB or migrations run only on the orchestrator/merge.
- **G3 (claimable invariant):** a topic with zero nodes in a dispatchable state is never `ready`/claimable.
- **G4 (reconcile/decommission safety):** `reconcile` must not demote a topic with a live healthy agent (or must clean the agent); `decommission-agent` must never trigger topic verification.
- **G5 (atomic add-task):** `add-task` auto-creates its topic in one transaction — no empty-topic window.
- **G6 (cockpit label render):** populate the graph-panel node's `user_label` from its bound thread (fix `juggle_cockpit_model.py`), so the panel shows `XW` not `dc2e`; and converge `graph_nodes`/`graph_tasks` table references with the rename so `juggle_cockpit_model.py:219` doesn't silently degrade.

## Process change (codified 2026-06-13)
Added to `_AUTOPILOT_DIRECTIVE` (`src/juggle_hooks_autopilot.py`): the **DEFECT PROTOCOL** (defects outrank features: stop → freeze → RCA → plan → fix → resume) and the **`verified ⟺ merged to main`** invariant.
