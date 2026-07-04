# RCA — Integrate wedge + "verified⟺merged" alarm (2026-07-03)

**Author:** researcher (thread KH), READ-ONLY investigation.
**Workspace:** `/Users/mikechen/github/juggle` @ `main` (HEAD `abb0793`).
**Incident:** 2026-07-03 ~17:10 local. Three topics (`T-rail-color-palette`,
`T-cockpit-done-header`, `T-gp-migration`, project `P2`) wedged in
`state='integrating'` for 1–1.5 h with no integrate running; their member tasks
`state='verified'` but unmerged; three coder agents still `busy` after their
panes finalized. Watchdog was STOPPED by the orchestrator (frozen per defect
protocol) — not restarted.
**Sources (retrieved 2026-07-03):** `~/.claude/juggle/juggle.db` (nodes,
agents, agent_runs, action_items, notifications_v2, spool_journal — read-only
`sqlite3`); `~/.claude/juggle/watchdog-spawn.log`; the three worktrees under
`/private/tmp/juggle-juggle-{GO,GQ,GU}`; repo `src/`.

---

## TL;DR

1. **The "verified ⟺ merged-to-main" alarm is a MISREAD at task level.** By
   design (spec §2.3), a *task* `verified` means only *committed-in-worktree +
   `verify_cmd` green* — **NOT merged**. `verified ⟺ merged` holds at **topic**
   level only. Tasks verified-but-unmerged is expected, not the bug.
2. **The real defect: the three topics are WEDGED at `integrating`.** Topic
   state is *derived from member-task states* by `reconcile_topic_state`; when
   all tasks are `verified` but there is no merged-SHA, it derives `integrating`
   — and it derives this at **mark-task time, before integrate ever runs**.
3. **A single integrate miss = permanent wedge.** The only code that lands a
   merge for a topic is `cmd_complete_agent` → `_run_integrate`, run **once,
   inline**. If that miss occurs, nothing re-drives it: the watchdog has **no
   re-integrate driver** for an `integrating` topic, the repair sweep needs a
   `fail_envelope` (none was written), and orphan-reconcile skips a topic bound
   to a live/busy agent. The topic sits at `integrating` forever and counts as
   `in_flight`, silently.
4. **This is the recurrence of A9a / Defect C** flagged as an OPEN follow-up in
   `research/2026-07-02-juggle-rca.md` ("a durable reconcile-repair path is
   still an open follow-up") and `research/2026-07-01-integrate-machinery-defects.md`
   (Defect C). Same root-cause **family** as the df-* dispatch wedges: *state
   advanced/derived in one subsystem, decoupled from the effecting action, with
   no idempotent re-drive.*

---

## Evidence (ground truth)

Task/topic state (nodes):

| task (kind=task) | state | merged_sha | topic (kind=topic) | topic state | fail_envelope |
|---|---|---|---|---|---|
| gp-migration | verified | — | T-gp-migration | **integrating** | — |
| cockpit-done-header | verified | — | T-cockpit-done-header | **integrating** | — |
| rail-color-palette | verified | — | T-rail-color-palette | **integrating** | — |

Worktrees (git, retrieved 2026-07-03): all **clean, 1 commit ahead of `main`,
NOT an ancestor of `main`** (real committed work, never merged):
`GO@462f8d6`, `GQ@df3b12f`, `GU@442fbce`.

Dispatch bindings (`node_edges kind='dispatch'`) → busy agents:
`T-cockpit-done-header→dfb9810d→4db4e1cc` (busy_since 22:46), `T-gp-migration→
2e0a57a0→86c6d52d` (busy_since 23:34, no `current_run_id`), `T-rail-color-palette→
07f68285→09ca81dc` (busy_since 23:15). *(times UTC)*

Agent ledger (`agent_runs`): run **381 (cockpit) = `dispatched` (never
closed)**, **383 (rail) = `dispatched`**, **382 (gp) = `failed`** (stall
re-dispatch). `completed_at`/`after_sha`/`diffstat` all empty.

Action items: **zero** `failed-integration` / "stuck at integrating" /
"worktree finalization failed" items for the three topics (contrast: dozens of
historical `Topic X failed (failed-integration)` items exist — so that path
normally *does* file one). Only prior items: early **"Autopilot dispatch failed
… cannot dispatch coder task without an isolated worktree … Worktree
auto-create failed"** (dismissed after a successful retry).

Spool (`spool_journal`): `graph_mark_task` applied ×3 (22:51:10, 22:51:42,
23:33:31); `agent_complete` applied ×4 (22:53, 22:56, 23:36, 23:42 — gp
completed **twice**); `graph_mark_task 97bb1800` **superseded** (23:35).

Watchdog log: each "task X → verified" runs in a fresh CLI subprocess; prints
`Agent complete for Topic {GO,GQ,GU}`; **23:34** "coder [GQ] stalled — re-dispatched
09ea404d → 86c6d52d"; **no** integrate/rebase/test/refusal output for the three.

---

## Q1 — What marks a task `verified`? Is integrate a gate? (it is NOT, by design)

Path: `juggle graph mark-task <id>` → `cmd_graph_mark_task`
(`src/juggle_cmd_graph.py:209`) → spooled in agent context
(`spool_event_if_agent`, line 218) → replayed → `db_graph.mark_completion(
integrate_ok=True, verify_ok=not --fail)` (`src/dbops/db_graph_marking.py:27`)
→ walks the task to `integrating` then `task_transition("integrate_ok")` →
`verified`.

- **`integrate_ok` is HARD-CODED `True`** at line 228, and the docstring
  (`juggle_cmd_graph.py:212-214`) is explicit: *"task 'verified' =
  committed-in-topic-worktree + verify_cmd green — verified-means-MERGED holds
  at TOPIC level only (spec §2.3)."*
- There is **no merge gate at task level** (`mark_completion` never checks a
  SHA). The **topic** level *does* gate: `topic_transition` refuses
  `→verified` unless merged (`UnmergedVerifyRefused`, `src/dbops/db_topics.py:78`).

➡️ **Conclusion:** the incident premise ("tasks verified but not on main =
invariant violation") is a misread. Task-verified-but-unmerged is the designed
contract. The invariant that IS at risk (topic verified ⟺ merged) was **not
violated** — the topics correctly did *not* reach `verified`. They are wedged
one state short.

## Q2 — Why are the topics wedged in `integrating`? (ROOT CAUSE)

`reconcile_topic_state` (`src/dbops/db_topics_reconcile.py:126`) DERIVES topic
state from member-task states. Lines 159-175: when **all tasks `verified`** and
`_verified_allowed` is false (no merged-SHA ancestor of main) and
`_heal_merged_sha` can't find one → **`target = "integrating"`** and it writes
it (lines 195-202).

Two structural facts make this a trap:

1. **Derived at mark-task time, before integrate.** `cmd_graph_mark_task:238`
   calls `reconcile_topic_state` right after each task mark. So the topic is
   pushed to `integrating` at 22:51/23:33 — *independent of whether integrate
   ever runs*. Topic `updated_at` is ~0.09 s after each task's `verified_at`,
   matching this reconcile call. **Topic-state advance is decoupled from the
   integrate outcome.**

2. **The only merge-lander runs once, inline, and nothing re-drives it.** A
   real merge (and `merged_sha`) is produced only by `cmd_complete_agent` →
   `_run_integrate` (`src/juggle_cmd_agents_complete.py:58`), executed a single
   time during the `agent_complete` spool replay. For these three it did **not
   land** (worktrees intact & unmerged, `merged_sha` empty). After that miss:
   - `graph_tick` re-dispatches only **`ready`** topics; an `integrating` topic
     just increments `in_flight` forever (`src/juggle_graph_dispatch.py:218`).
   - `run_repair_sweeps` only picks up **`failed-integration` + `fail_envelope`**
     topics; `fail_envelope` is empty → skipped.
   - `reconcile_orphaned_inflight` explicitly **skips a node bound to a live
     (busy) agent** (`juggle_graph_dispatch.py:186-192`) — and the agents are
     `busy`.
   - `flag_unmerged_completed_topics` (G5, watchdog Loop 2b,
     `juggle_watchdog_daemon.py:298`) only *surfaces* such topics — and here
     filed **no** action item (grace/dedup or the frozen watchdog).

   ➡️ **There is no watchdog re-integrate driver.** Integrate is documented as
   "watchdog-owned" (`juggle_cmd_integrate.py:290`) but the watchdog owns only
   *dispatch* and *repair-of-failed*, never *retry-of-integrating*. One miss =
   permanent wedge.

3. **Secondary aggravator — reconcile can erase a failure verdict.**
   `reconcile_topic_state` guards only `verified` from re-derivation (line 146);
   it does **not** guard `failed-integration`/`failed-verify`. Because all tasks
   are terminal-`verified`, any later reconcile re-derives `integrating` and
   would **overwrite** a `failed-integration` the completion had set. This
   removes the failure signal (and its repair path) even when integrate *does*
   fail loudly.

**Why did this integrate miss (the trigger)?** — *PLAUSIBLE, not fully proven.*
`fail_envelope` is empty (so `_run_integrate` did **not** hit a `record_refusal`
`_fail` path — rules out rebase-conflict / test-failure / dirty / empty-branch),
AND no `failed-integration` action item exists (so `mark_graph_topic` did not
transition the topic to `failed-integration`), AND no "stuck at integrating"
self-heal item exists (so the `UnmergedVerifyRefused` branch's item, if it ran,
was not persisted). The three had prior **"Worktree auto-create failed — branch
cyc_GS/GT already exists"** dispatch failures (a worktree/branch-collision
dispatch wedge, kin to df-atomic-dispatch), retried into the current worktrees.
Most consistent reading: the `agent_complete` replay ran, integrate did not
land a merge, and the topic was left at the `integrating` value the mark-task
reconcile had already written — i.e. the completion's topic transition
effectively no-op'd against an already-`integrating` topic while the merge never
happened. The load-bearing defect does not depend on which of these it was: **a
topic derived to `integrating` with no merge and no live re-integrate driver is
stuck regardless.**

## Q3 — Why are the agents still `busy` after finalize?

The `agent_runs` ledger is decisive: runs 381 (cockpit) and 383 (rail) are still
`dispatched`; 382 (gp) is `failed`. `cmd_complete_agent` sets the pool row idle
and closes the ledger **only when `get_agent_by_thread(thread)` returns a busy
agent whose `assigned_thread` matches** (`src/dbops/agents.py:168` filters
`status='busy' AND assigned_thread=?`; guarded at
`juggle_cmd_agents_complete.py:132-144`). The completion runs in **spool
replay**, by which time the agent↔thread binding can have shifted (the harness
pane keeps running after the agent *spools* its completion; `last_activity` for
4db4e1cc is 23:58 — 1 h past its "completion"). When the binding has moved the
idle-write silently no-ops (`if agent:`), leaving the pool `busy` and the ledger
open. On gp this was compounded: the watchdog **stall detector re-dispatched**
09ea404d→86c6d52d at 23:34, marking run 382 `failed` and a fresh agent `busy`.

➡️ **Same wedge family, not a separate root defect:** pool `busy`/ledger state
is decoupled from completion because completion effects are conditional on a live
binding that the spool-replay + stall-redispatch churn invalidates.

## Q4 — Is the spool replay implicated? (the "superseded" line)

The `graph_mark_task 97bb1800 superseded — task 'gp-migration' already in
terminal state 'verified'` (23:35) is a **benign idempotency no-op**
(`src/juggle_spool_apply.py:118 _superseded_replay`), a **symptom not a cause**:
the post-stall re-dispatched agent (86c6d52d) re-marked the already-verified gp
task. Spool *is* structurally implicated though: the `graph_mark_task` replay is
what marks tasks `verified` and reconciles the topic to `integrating`
**before/without** integrate — task-state advance (spool `graph_mark_task`) is
decoupled from integrate (spool `agent_complete`). (Note: earlier in the day
many spool events **dead-lettered** 10:28–11:28 — the A5/A6 spool-isolation
defects from the prior RCA — but the three events here **applied**, so
dead-lettering is not the direct cause of this wedge.)

## Q5 — Relation to the 2026-07-03/02 dispatch-wedge defects

**Same family, downstream manifestation — and specifically the recurrence of a
KNOWN open follow-up:**

- **A9a** (`research/2026-07-02-juggle-rca.md`): *"Cross-topic dep blocked by a
  stale `integrating` topic … Root-cause class = the record-before-push
  NULL-`merged_sha` wedge … a durable reconcile-repair path is still an open
  follow-up."* — This incident is that exact wedge, un-fixed, hit three topics
  at once.
- **Defect C** (`research/2026-07-01-integrate-machinery-defects.md`): merged_sha
  unrecorded → topic wedged at `integrating`; `_heal_merged_sha` is the partial
  mitigation but only works when the branch tip **is** already an ancestor of
  main (ours are not).
- **df-atomic-dispatch kin:** the trigger ("branch cyc_GS/GT already exists /
  worktree auto-create failed") is a dispatch-time worktree/branch collision, the
  same *non-idempotent side-effect* anti-pattern as the df-* dispatch wedges.

The unifying anti-pattern: **derived/advanced state in one subsystem, decoupled
from the effecting action, with no idempotent re-drive** — task marks and
reconcile advance topic state, but integrate (the effecting action) is a
one-shot with no retry loop.

---

## Proposed fix plan (do NOT implement — RCA only)

Ordered by leverage:

1. **Add a watchdog re-integrate driver (closes A9a).** In the tick, sweep
   topics in `integrating` whose (a) worktree branch still has commits ahead of
   main, (b) `merged_sha` is empty, and (c) bound agent is **not** live; and
   idempotently re-run `_run_integrate` (or, on a real failure, route to the
   repair sweep by writing a `fail_envelope`). This is the missing "durable
   reconcile-repair path." Owner: `juggle_graph_dispatch.graph_tick` /
   `juggle_graph_repair`.
2. **Guard failure verdicts in `reconcile_topic_state`
   (`db_topics_reconcile.py:146`).** Do not re-derive a topic out of
   `failed-integration`/`failed-verify` back to `integrating` from all-verified
   tasks — add these to the terminal-guard alongside `verified`. Prevents the
   failure signal (and its repair trigger) from being erased.
3. **Make completion effects unconditional on a live binding
   (`juggle_cmd_agents_complete.py:132-144`).** Close the ledger by `thread_id`
   and reap the agent by the run's recorded `agent_id`, not only via
   `get_agent_by_thread` (which requires the still-live busy binding). Prevents
   spool-replay-after-rebind from leaving agents `busy` and runs `dispatched`.
4. **Fix the auto-dismiss window
   (`juggle_cmd_agents_complete.py:63` vs `79`/`196`).** The finalization-failure
   action item is created (line 63) *before* the `items_to_dismiss` snapshot
   (line 79), so it is auto-dismissed (line 196). Snapshot BEFORE creating any
   new items — otherwise a genuine integrate failure is silently swallowed.
5. **Make the G5 orphan guard actually surface this
   (`juggle_watchdog_daemon.py:298`, `dbops/orphan_guard`).** Verify the
   grace/dedup does not suppress a >1 h wedge; a HIGH action item should exist
   after the grace window. Three topics wedged for 1.5 h produced zero visible
   items.
6. **Root-fix the dispatch trigger.** Make worktree/branch creation idempotent
   (clean up stale `cyc_*` branches or reuse on collision) so "branch already
   exists" no longer forces the retry churn that precedes these wedges.

### Manual recovery for the current wedge (operator, once fix is in / by hand)
For each topic: with the watchdog frozen, run `juggle integrate <thread>`
(`--allow-legacy-agent-integrate` from an operator shell) against the preserved
worktree; on green it records `merged_sha`, removes the worktree, and reconcile
completes `→verified`. Then reap the three busy agents and close their open
`agent_runs`. (Do NOT hand-stamp `merged_sha` — the branches are genuinely
unmerged; a real ff-merge is required.)
