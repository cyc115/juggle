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

## Fix status

| RC | Status | Owner |
|---|---|---|
| RC1 — inline gate death by watchdog respawn | **Fixed** (this branch) | `T-fix-complete-detached-integrate` |
| RC2 — bound-agent guard has no completion escape | Open — follow-up | — |
| RC3 — release ignores `agent_completions` | Open — follow-up | — |
| RC4 — `_backoff` wiped by restarts | Open — follow-up | — |
