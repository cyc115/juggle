# M4 — integration record (proof states → ledger; 6-state machine complete)

> **For agentic workers:** execute with superpowers:executing-plans. Depends on
> M1 + M3 merged into `juggle-2.0`. HIGH RISK — the verified⟺merged invariant
> lives here; devil's-advocate pass is mandatory before coding, and the
> migration-61 pin rewrite needs extra care.

**Goal:** remove `verified`, `integrated-unlanded`, `delivered` from the node
machine. A node stays `integrating` until fully done. Proof steps become an
append-only **integration record** in ONE module; `done` is reachable only when
the record satisfies its proof. Final machine (6): open, running, integrating,
done, failed(kind), archived. (`background` remains conversation-only vocab via
the M2 projection.)

**Why:** decision log §8.3. Invariant to preserve at full strength:
**a topic is done-with-merge ⟺ its record has `landed(sha)`** — the migration-61
incident (topic wedged `integrating` forever; `pending_merged_sha`) is the
canonical failure this guards.

## Files Touched

| File | Action |
|---|---|
| `src/dbops/integration_record.py` | Create — THE single pipeline module: steps `tests_green → submitted → landed(sha) → g1_pass` (+ `attested(verify_cmd)` for non-merge/deliver topics), append-only, ordered, fail-loud on out-of-order append |
| `src/dbops/db_node_machine.py` | Modify — drop proof states; `(integrating,"finalize")→done` gated by record-proof check |
| `src/dbops/db_topics.py`, `db_topics_marking.py`, `db_graph_marking.py`, `db_reintegrate.py` | Modify — emit record steps instead of state walks |
| `src/juggle_watchdog*.py` (integrate flow), land-poller | Modify — read/append record steps |
| `src/dbops/orphan_guard.py` | Modify — wedge detection from step timestamps (finer than state+duration) |
| `src/dbops/migration_<next>_*.py` | Create — rewrite existing proof-state rows into records + `done`/`integrating` |
| `tests/` | Rewrite migration-61 pin + verified⟺merged pins against the record |

## Tasks (sequential)

- [ ] **1. Inventory (read-only).** Every read/write of the three proof states
  and of `merged_sha`. Checklist into PR description.
- [ ] **2. Record module, test-first.** Pure append API:
  `record_step(topic_id, step, **facts)`; ordering enforced by table (illegal
  predecessor = `InvalidStep`, fail-loud); `proof_satisfied(record,
  delivery_kind) -> bool` pure function. RED tests: order enforcement; merge
  topics need `landed.sha`; deliver topics need `attested`; async-land =
  `submitted` without `landed` blocks done (the integrated-unlanded window,
  now a record shape). ≤300 lines.
- [ ] **3. Machine cut-over.** Replace state walks with step appends +
  single `finalize` transition gated on `proof_satisfied`. `g1_pass` becomes a
  step. RED test: `finalize` without proof raises; merged_sha is written
  ONLY from the `landed` step (grep-proof: no other writer).
- [ ] **4. Consumers.** Land-poller promotes by appending `landed`; reports/
  cockpit derive display phase from the record's last step. Orphan guard:
  wedge = open record whose last step is older than threshold — rewrite the
  hard-wedge escalation (`orphan_guard.py:200`) against it.
- [ ] **5. Migration.** `verified`→done+record(landed,g1_pass);
  `integrated-unlanded`→integrating+record(...submitted);
  `delivered`→done+record(attested). Fixture test with one row of each.
- [ ] **6. Pin rewrites.** Migration-61 pin: same symptom ("wedged integrating
  is detected and repairable"), new seam. verified⟺merged pin: "done with
  merge-delivery and no landed.sha is unrepresentable". Docstrings keep
  incident dates.
- [ ] **7. Sweep.** Delete dead proof-state vocab everywhere; mechanical
  commit separate.

## Enforcement

Proof gate = code in ONE module (strong; scattering it is the failure mode —
reviewer must reject any second `proof_satisfied` implementation). Step order =
schema + code. merged_sha single-writer = grep-proof test. Nothing
prompt-enforced.

## Definition of done

Full pytest green incl. rewritten pins; doctor migrates seeded 1.x DB with all
three proof states represented; integrate E2E test (dispatch→land→done) green;
paste suite summary line.
