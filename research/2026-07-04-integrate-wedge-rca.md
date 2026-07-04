# Integrate-wedge #2 — RCA (2026-07-03/04)

**Incident window:** completions 22:24–22:45 → wedge detected 23:10 → manual
force-release 23:13 → sweep re-drive 23:14:51 → `cyc_VS` landed ~23:19.

**Symptom:** three topics — `T-gp-cancel`, `T-gp-retry`, `T-gp-edit` — sat stuck
in `state='integrating'` for 45+ minutes, each holding a bound coder agent
`busy`, so the agent pool drained and no new topics could dispatch. This is the
SECOND integrate-wedge incident (the first, 2026-07-03, produced the watchdog
re-integrate sweep in `juggle_graph_reintegrate`).

Four independent root causes compounded. RC1 is fixed by
`T-fix-complete-detached-integrate` (this branch); RC2–RC4 are documented here
for follow-up.

---

## Timeline

| Time (approx) | Event |
|---|---|
| 22:24–22:45 | `T-gp-{cancel,retry,edit}` coder agents call `complete-agent`; each spools an `agent_complete`, applied inside the watchdog process. Each runs the merge gate INLINE. |
| 22:34, 22:43 | watchdog logs `plugin code advanced → exiting cleanly for respawn` (watchdog-spawn.log) — a *successful* integrate advanced plugin HEAD, tripping the respawn, killing the in-flight inline gate mid-run. |
| 22:45–23:10 | topics remain `integrating`; the re-integrate sweep never re-drives them (RC2 bound-agent guard; RC4 backoff wiped). Agents stay `busy` (RC3). |
| 23:10 | wedge detected (pool exhausted, no dispatch progress). |
| 23:13 | manual force-release of the three bound agents. |
| 23:14:51 | sweep re-drives the now-unbound topics; detached integrates spawn. |
| ~23:19 | `cyc_VS` lands; reconcile advances topics to `verified`. |

---

## RC1 — complete-agent runs the merge gate INLINE (this fix)

**Location:** `juggle_cmd_agents_complete.py:67` — `cmd_complete_agent` called
`juggle_cmd_integrate._run_integrate(thread, db)` directly, inside the
spool-apply.

**Mechanism:** an autopilot coder's `complete-agent` is SPOOLED and applied by
the **watchdog process**. The watchdog self-restarts whenever plugin HEAD
advances (`plugin code advanced → exiting cleanly for respawn`,
watchdog-spawn.log 22:34/22:43). Every *successful* integrate advances HEAD — so
restarts are frequent, and each one kills any in-flight inline merge gate
(~7 min full suite + rebase + push). The topic is left `integrating` with no
process finishing the transition.

This is the same class of bug the 2026-07-03 incident hit on the watchdog TICK
(inline gate blew the 90s tickguard budget → daemon restart mid-gate). The fix
there was the detached re-integrate sweep. RC1 is the completion-time twin: the
sweep recovered wedged topics, but complete-agent kept *creating* them.

**Fix:** complete-agent marks the bound TOPIC `integrating` and spawns the SAME
detached integrate the sweep uses
(`juggle_integrate_spawn.spawn_detached_integrate`: `start_new_session=True`,
`JUGGLE_ORCHESTRATOR=1`, output → `~/.claude/juggle/reintegrate-spawn.log`), then
returns WITHOUT waiting. The watchdog reconcile/reintegrate tick applies the
verdict from git reality (landed → `verified`; `fail_envelope` →
`failed-integration`). A shared spawn helper (`juggle_integrate_spawn`) is used
by both callers so the command shape / detachment / env can never drift apart.
Legacy non-topic threads (interactive completions, not watchdog-driven) keep the
inline `_run_integrate` / `_finalize_worktree` path.

**Invariant established:** nothing merge-landing may EVER run inline in the
watchdog/spool process.

**Regression pins:** `tests/test_complete_detached_integrate.py` — asserts the
detached spawn (correct `integrate <thread>` cmd shape, `start_new_session=True`,
watchdog-owned env), that the inline gate is never called, that
`cmd_complete_agent` returns while the gate is still running (`poll() is None`),
and that legacy non-topic threads still finalize inline. Mirrors the
detached-integrate spawn pins from `7c834df`.

---

## RC2 — re-integrate bound-agent guard has no completion escape

**Location:** `juggle_graph_reintegrate.py:221` — `_reintegrate_topic` returns
early when `_has_live_bound_agent(db, thread_id)` is true ("owning agent may
still be finalizing — never re-drive under it").

**Mechanism:** the three wedged topics still had their coder agents bound `busy`
(RC3). The guard therefore skipped them on every tick, so the sweep — the
designed recovery path — never fired until the agents were manually released at
23:13. The guard has no escape for the case where the agent has *already
completed* (spool applied) but was never released: a completed-but-still-bound
agent looks identical to a mid-finalize one.

**Proposed follow-up:** treat the presence of a recorded `agent_completion` for
the thread as a completion signal that overrides the live-bound guard (drive the
topic even though an agent row is still bound busy), or have the guard consult
agent liveness (heartbeat/pane) rather than the mere existence of a binding.

---

## RC3 — agent release ignores `agent_completions`

**Location:** `juggle_cmd_agents_lifecycle.py:175-181` — the agent-release path.

**Mechanism:** after a clean completion the coder agents stayed `busy` because
release keyed on the live `get_agent_by_thread` binding and never consulted the
recorded `agent_completions`. In a spool-replay-after-rebind the binding had
shifted, so the idle-write silently no-op'd (the same shape as the
2026-07-03 Q3 fix in `test_complete_agent_wedge_fixes.py`, but on the *release*
side). Busy-but-done agents drained the pool and, via RC2, blocked the sweep.

**Proposed follow-up:** release must reconcile against `agent_completions` — an
agent with a recorded completion for its (last) bound thread is idle regardless
of the current binding.

---

## RC4 — in-memory `_backoff` dict wiped by restarts

**Location:** `juggle_graph_reintegrate.py:85-110` — `_backoff` is a module-level
`dict` keyed by `(db_path, topic_id)`, holding attempt counts + last-attempt
timestamps + the spawned proc handle.

**Mechanism:** the watchdog restarted repeatedly (RC1's HEAD-advance respawns).
Each restart re-imports the module and resets `_backoff = {}`, discarding attempt
counts and the single-flight proc handle. Consequences: the
`MAX_REINTEGRATE_ATTEMPTS` soft-failure backstop can never accumulate across
restarts (a topic could re-spawn forever without ever escalating to
`failed-integration`), and the single-flight `_spawn_alive` guard is lost, so a
post-restart tick can spawn a duplicate integrate (tolerated only because the
per-repo lock serializes them).

**Proposed follow-up:** persist re-integrate attempt state durably (a topic
column or a small table) so the backstop + backoff survive restarts, instead of
holding it in process memory that every respawn erases.

---

## RC5 — integrate lock phantom-holder race (the worst one)

**Incident:** 2026-07-03/04 integrate-wedge #2, RC5 — ~30 min full merge-queue
outage, 9 deadlocked gate processes. Observed live with PIDs 3893/3899 spawned
70 ms apart.

**Location:** `juggle_integrate_lock.py` — `acquire_repo_lock`, the old
shared-tmp + rename-replace acquisition.

**Mechanism (four compounding steps):**

1. **Shared tmp path.** ALL acquirers wrote the same
   `lock_path.with_suffix(".lock.tmp")`. A writes `tmp(pid_A)`; B overwrites
   `tmp(pid_B)`; A renames `tmp → lock`. POSIX `rename(2)` silently REPLACES,
   so the lockfile now records **pid_B** even though A performed the rename.
2. **Winner disowned.** A's post-rename verify (`pid == os.getpid()`) fails →
   A loops and starts waiting on "B".
3. **Waiting on yourself.** B's own rename fails `ENOENT` → B loops, reads the
   lock, sees a LIVE pid — **its own** — and waits on itself. There was no
   "holder is me" check.
4. **No heartbeat, no steal.** The heartbeat thread starts only after a
   successful verify, so nobody heartbeats (lock timestamp observed 20 min
   stale). `_pid_alive`-based steal never fires because the recorded pid is a
   live (deadlocked) process. Every subsequent gate queues behind a lock
   **nobody holds** until the recorded process hits its 1800 s timeout and dies.

**Trigger amplifier:** the reintegrate sweep spawns one detached gate per
TOPIC, and `T-gp-retry` + `T-gp-edit` share one thread/branch (dual-dispatch),
so every sweep round launched a same-instant PAIR racing the lock.

**Fix (this task — three layers):**

a. **Atomic exclusive create.** Acquire via
   `os.open(lock_path, O_CREAT|O_EXCL|O_WRONLY)` and write pid+ts through the
   fd. The shared-tmp + rename scheme is deleted entirely — rename-replace can
   never be exclusive on POSIX, so exactly one racer becomes the holder and the
   winner's own pid is always what the file records.
b. **Self-hold detection.** If the lockfile records the CURRENT pid but this
   process holds no live heartbeat for it, treat it as a poisoned lock — remove
   and retry (belt-and-braces; unreachable given (a), logged loudly).
c. **Phantom detection for waiters.** A live-pid lock whose timestamp is stale
   beyond `PHANTOM_HEARTBEAT_MULTIPLE` (10×) the heartbeat interval means the
   recorded holder never started heartbeating — log loudly and steal. This
   refines DA M2's "never steal a live pid": that rule assumed the holder
   heartbeats; a live pid with a dead heartbeat is by construction NOT mid-gate.

Atomic create opens a µs window between the O_EXCL create and the pid write
where a racer could read the file EMPTY. Stealing a half-written lock would
double-hold, so an unparseable lock (`pid <= 0`) is given a
`CORRUPT_LOCK_GRACE_SECS` grace (a live writer finishes in µs) and only stolen
if it stays corrupt — never instantly.

**Regression pins:** `tests/test_integrate.py` —
`test_lock_concurrent_acquirers_one_wins_no_phantom_holder` (multiprocessing
hammer: winner's own pid always recorded),
`test_lock_self_pid_unverified_is_recovered_not_waited_on`,
`test_lock_live_pid_stale_heartbeat_is_stolen`,
`test_lock_empty_midwrite_not_stolen_no_double_hold`, and the refined
`test_lock_live_holder_is_never_stolen_pin` (fresh heartbeat → never stolen).

---

## Fix status

| RC | Status | Owner |
|---|---|---|
| RC1 — inline gate death by watchdog respawn | **Fixed** (this branch) | `T-fix-complete-detached-integrate` |
| RC2 — bound-agent guard has no completion escape | Open — follow-up | — |
| RC3 — release ignores `agent_completions` | Open — follow-up | — |
| RC4 — `_backoff` wiped by restarts | Open — follow-up | — |
| RC5 — integrate lock phantom-holder race | **Fixed** (this branch) | `T-fix-integrate-lock-phantom-holder` |
