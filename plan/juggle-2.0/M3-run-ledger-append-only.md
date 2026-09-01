# M3 — run ledger append-only + derived agent pool

> **For agentic workers:** execute with superpowers:executing-plans. Depends on
> M1 merged into `juggle-2.0`.

**Goal:** the run ledger (`dbops/runs.py`, `agent_runs`) stops being a state
machine: no mutable `status` — a run is open iff `ended_at IS NULL`, closed by
appending facts. Agent `idle`/`busy` becomes a derived predicate over open
runs for supervisor-managed harnesses; the `agents` table survives ONLY as the
Claude/Codex legacy path (deletion gated on retiring those harnesses —
decision 4).

**Why:** decision log §8 decision 4 + the documented race: run stuck
`'dispatched'` while its topic reached `integrating`
(`dbops/db_topics_marking.py:188`).

## Files Touched

| File | Action |
|---|---|
| `src/dbops/schema_runs.py` | Modify — add `ended_at`, `outcome`, `session_id`; keep `status` column readable, stop writing (rollback-cheap) |
| `src/dbops/runs.py` | Modify — `open_run`/`close_run` → append semantics; open-run query by `ended_at IS NULL` |
| `src/dbops/db_topics_marking.py`, completion paths | Modify — single close choke point stays; no status races |
| `src/dbops/agents.py` | Modify — add derived `agent_busy(thread_id)` predicate; document legacy-only scope of `idle`/`busy` rows |
| `src/dbops/migration_<next>_*.py` | Create — backfill `ended_at`/`outcome` from `status`+`completed_at` |
| `src/dbops/orphan_guard.py` | Modify — "open run too long" reads `ended_at IS NULL` + duration |
| `tests/` | Rewrite the stuck-'dispatched' pin to the new seam; add close-idempotency pin |

## Tasks (sequential)

- [ ] **1. Inventory (read-only).** Grep `agent_runs` status reads, `close_run`
  callers, `agents.status` consumers. Checklist into PR description.
- [ ] **2. Schema + append semantics.** Migration adds columns; `close_run`
  becomes idempotent append (second close = no-op, logged). RED tests:
  idempotent close; open-run predicate; the 2026-07 stuck-'dispatched' shape
  is unrepresentable (topic completes ⇒ same transaction stamps `ended_at`).
- [ ] **3. One choke point.** Verify ALL completion paths (`mark_graph_*`,
  `agent complete/fail`, watchdog recovery) funnel through the single close
  function — grep-proof in PR. Move any stragglers.
- [ ] **4. Derived pool predicate.** `agent_busy` = open run exists for the
  binding. Supervisor-facing dispatch (M5) will use it; Claude-harness path
  keeps CAS rows untouched. Add `dispatcher_lease` table (single row: owner
  id + heartbeat) — created now, enforced in M5. RED test: lease CAS.
- [ ] **5. Token columns.** `close_run` keeps best-effort token capture;
  add nullable `session_ref` so M5 can attribute via pi session stats instead
  of transcript parsing. No behavior change for Claude runs.
- [ ] **6. Pin rewrites + sweep.** Rewrite ledger-race pins; mechanical
  refactor commit separate (`runs.py` stays ≤300 lines — split if not).

## Enforcement

Append-only = schema + idempotent close (code). Single choke point =
grep-proof in PR + a test asserting no other writer touches `ended_at`.
Lease = schema now, code-enforced in M5. Nothing prompt-enforced.

## Definition of done

Full pytest green; doctor migrates seeded 1.x DB (runs backfilled correctly —
fixture asserts counts); paste suite summary line.
