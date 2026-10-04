# M1 — node machine collapse (failure merge + ready/dispatching removal)

> **For agentic workers:** execute with superpowers:executing-plans.
> Test-first: every behavior change lands RED → GREEN. Run the devil's-advocate
> pass before coding (Working Rules, CLAUDE.md).

**Goal:** shrink `dbops/db_node_machine.py` from 16 states toward the 6-state
target: merge the four failure states into `failed` + `failure_kind`; make
readiness a derived predicate; remove `dispatching`; fold `cancelled` into
`archived` + `archive_reason`. The integrating-side proof states
(`verified`, `integrated-unlanded`, `delivered`) are OUT OF SCOPE — M4 removes
them.

**Why:** decision log §8.1/§8.4 (`research/2026-08-07-pi-harness-migration.md`).
Evidence the merges are safe: all four failure states have identical exits
(`db_node_machine.py:72-92`); `_DISPATCHABLE_TASK_STATES=("open","ready")`
shows `ready` is already half-derived (`db_topics.py:279`).

## Files Touched

| File | Action |
|---|---|
| `src/dbops/db_node_machine.py` | Modify — new transition table |
| `src/dbops/db_topics.py`, `db_graph*.py`, `db_topics_marking.py`, `db_topics_reconcile.py` | Modify — event call sites |
| `src/dbops/migration_<next>_*.py` | Create ×2 (failure merge; ready/dispatching/cancelled) — reserve numbers via `juggle migration next` |
| `src/juggle_watchdog*.py`, `src/juggle_cmd_agents_*.py` | Modify — playbook selection reads `failure_kind` |
| `tests/` | Rewrite affected pins; add new pins |

## Tasks (sequential)

- [ ] **1. Inventory (read-only).** Grep every literal use of
  `failed-exec|failed-integration|failed-verify|blocked-failed|ready|dispatching|cancelled`
  across `src/` and `tests/`. Emit checklist into the PR description. No edits.
- [ ] **2. Failure merge.** Add `failure_kind` column (migration). New table
  rows: `(running,"exec_fail")→failed` etc., all recording kind; exits
  `(failed,"reload")→open`, `(failed,"archive")→archived`. Update watchdog
  playbook selection to branch on `failure_kind` — behavior byte-identical.
  RED test first: playbook chosen per kind. Migration rewrites existing rows.
- [ ] **3. Derived readiness.** Delete `ready` state + `deps_ready`/`unready`
  events. Dispatch query computes eligibility (all deps `done`) inline;
  claim becomes CAS `open→running` stamping `dispatched_at` + owner. Delete
  `dispatching` + `stale_reset`; spawn failure reverts `running→open` via a
  new `spawn_fail` event (loud, logged). RED tests: double-claim race (two
  concurrent claims, one winner); spawn-fail revert.
- [ ] **4. Cancelled fold.** `cancel` event → `archived` with
  `archive_reason='cancelled'`; `reload` legal ONLY from archived rows with
  that reason (explicit table entry, not a conditional). Migration rewrites
  `cancelled` rows. RED test: resurrect path.
- [ ] **5. Pin rewrites.** Migration-51 vocabulary pin and any pin asserting
  the deleted states now assert the SAME incident-invariants through the new
  seam (e.g. "no row may hold a state the engine cannot transition"). Docstrings
  keep original incident dates. Never delete a pin — rewrite it.
- [ ] **6. Sweep + refactor pass.** Remove dead vocab from
  `node_translation.py` consumers, cockpit filters, `p8_readiness.py`.
  Separate mechanical commit.

## Enforcement

Transition table + `InvalidTransition` = code-enforced (strong). `failure_kind`
gets a CHECK constraint (schema). No prompt-enforced behavior in this milestone.

## Definition of done

Full pytest green incl. rewritten pins; `doctor --dry-run` migrates a seeded
1.x DB fixture correctly (add fixture test); conformance suite green; paste
suite summary line. States remaining after M1: open, running, integrating,
integrated-unlanded, verified, delivered, done, failed, background, archived.
