# M2 — thread status as projection (kill the second vocabulary)

> **For agentic workers:** execute with superpowers:executing-plans. Depends on
> M1 merged into `juggle-2.0`.

**Goal:** delete the thread state vocabulary
(`threads._VALID_STATES = {"active","running","closed","archived"}`) and the
translation table (`dbops/node_translation.py`). Thread/conversation status
becomes a read-only projection of the node state. One vocabulary everywhere.

**Why:** decision log §8 + verified debt: migrations 51/54/55/75 are all scars
of the two vocabularies drifting (`dbops/migration_75_conv_label_archived_only.py`,
2026-07-09 double-row incident).

## Files Touched

| File | Action |
|---|---|
| `src/dbops/threads.py` | Modify — drop state-machine mixin parts; `set_thread_status` becomes node-event application; status reads become projection |
| `src/dbops/node_translation.py` | Delete (fold any residual mapping into one function in `threads.py`, then inline) |
| `src/dbops/slug_alloc.py` | Modify — `LIVE_SLUG_STATES` → node-vocab only |
| `src/dbops/migration_<next>_*.py` | Create — drop/ignore `threads.status` column authority; backfill node rows where missing |
| `src/juggle_cockpit_model.py`, brief/report modules | Modify — read projection |
| `tests/` | Rewrite pins 51/54/75-adjacent |

## Tasks (sequential)

- [ ] **1. Inventory (read-only).** Grep `status` reads/writes on threads +
  every import of `node_translation`. Checklist into PR description.
- [ ] **2. Projection function.** Single helper: `thread_status(node_state) ->
  display status` (pure, ≤20 lines). RED tests: every node state maps; unknown
  state raises (fail-loud, no silent default).
- [ ] **3. Write-path unification.** All writers call node events; no code
  writes `threads.status`. Migration makes the column non-authoritative
  (keep column, stop writing — cheap rollback; a later cleanup drops it).
- [ ] **4. Read-path swap.** Cockpit, briefs, `list`/count queries read the
  projection. RED test: the migration-75 incident shape (done + background
  twin rows) can no longer double-count — same invariant, new seam.
- [ ] **5. Pin rewrites + sweep.** Rewrite vocabulary-parity pins (51/54/75)
  against the projection. Delete `node_translation.py`. Mechanical commit
  separate.

## Enforcement

Projection = code (pure function, fail-loud). Column demotion = migration
(schema). Nothing prompt-enforced.

## Definition of done

Full pytest green; cockpit viewport smoke (`cockpit --smoke --all-viewports`)
passes — status rendering is a TUI surface; doctor migrates seeded 1.x DB;
paste both summary lines.
