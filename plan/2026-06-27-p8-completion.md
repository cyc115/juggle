# P8 Completion — Finish the Unified-Nodes Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish P8 so the end-state has exactly ONE data model (`nodes`/`node_edges`) and ONE state machine (`db_node_machine.node_transition`) — retiring the legacy `threads`/`graph_topics`/`graph_tasks`/`graph_edges` tables and the `db_graph`/`db_mirror`/`db_topics` legacy engines.

**Architecture:** A strangler-fig migration currently parked at its maximum-debt midpoint (dual-write + dual-read of two models; the unified machine is dead code). This plan drives it to completion in 6 ordered steps, each independently shippable, each monotonically reducing a named legacy-surface counter, with the FULL `pytest` suite GREEN at every commit. The flip is sequenced by the REAL atomic clusters (task-execution, conversation, graph-topic) — not the fictional §13 per-phase slices — because a read-source flip and its consumers' column rename are inseparable and must co-commit.

**Tech Stack:** Python 3, SQLite (presence-based idempotent guarded migrations, no version ledger), pytest (`-n auto`), `juggle doctor --pre-p8-check --json` as the agent-verifiable gate.

> **Revision 2 (2026-06-27) — addresses Plan-DA round 2 (R2-1..R2-6, `specs/reviews/2026-06-27-p8-spec-da.md`).** Changes from v1: (R2-1, CRITICAL) `background` is now a **first-class node state** with a **bijective** `status↔state` map — the v1 plan treated the lossy `background→running` collapse as a mechanical value-rename, which breaks the watchdog reaper and the two distinct cockpit panels; the conversation cluster now includes `watchdog`/`dispatch_core`/`cmd_context`/`context_startup`. (R2-2) Engine-delegation Tasks 1.3+1.4 are now ONE atomic commit (dangling `_TRANSITIONS`/`_EVENTS`/`_cx` import fixed in-commit). (R2-3) Step 4 gains an explicit pre-flip backup + reverse-backfill rollback. (R2-4) Migration 51 is fail-LOUD. (R2-5) Task 4.2's `db_mirror` caller list now includes `threads.py` + `cmd_threads.py`. (R2-6, accepted) M44/M51 double-migration noted.

## Global Constraints

- **Full suite green at EVERY commit.** `make test` (== `uv run pytest -q`, `-n auto`) must pass before each commit. The `slow` marker tiers ONLY `make test-fast`; bare `pytest` and integrate run the FULL suite. A subsetting `test_cmd` is rejected fail-loud.
- **Env vars (read at import, no defaults):** `CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"`, `JUGGLE_MAX_BACKGROUND_AGENTS=5`, `JUGGLE_MAX_THREADS=10`. Export before running tests.
- **Migrations:** presence-based idempotent guarded functions (`PRAGMA table_info` / `sqlite_master` checks), no `schema_version` table. Additive/value migrations follow the fail-SOFT convention (`try/except sqlite3.OperationalError → _log.warning`). Destructive table-rebuild/drop migrations follow the fail-LOUD `BEGIN IMMEDIATE` convention (Migration 45, `migration_selfheal_status_check.py:35-69`). Highest existing migration = **50**; the next free number is **51**. **Exception (R2-4):** a value migration that a *same-release behavior change* hard-depends on — e.g. Migration 51 (`pending→open`), which the renamed engine requires before it can read/transition any task row — is CORRECTNESS-CRITICAL and MUST follow the fail-LOUD `BEGIN IMMEDIATE` convention, NOT fail-soft. A swallowed skip would silently strand rows the new code can neither see nor transition (tasks stall with no error).
- **Migration entrypoint:** all migrations auto-run via `JuggleDB.init_db()` → `run_migrations(conn)` (`dbops/migrations.py:22`), called by `doctor` in the orchestrator (non-agent) context behind `assert_migration_allowed` (G2). Never add an agent-reachable drop path.
- **LOC / architecture gate:** target ≤300 lines/module. When a touched file has outgrown its purpose, EXTRACT first (separate refactor commit, tests green) THEN edit. Pure-mechanical refactor commits are separate from behavior commits.
- **Regression-pin gate:** every bug/regression fix adds a pinned test that (a) fails RED on pre-fix code, (b) names the incident (date + symptom) in its docstring, (c) lives in the standard suite. Pins may not be deleted/weakened without explicit user approval.
- **Vocabulary (baked decision):** ONE task-entry state = `'open'`. `'pending'` is deleted everywhere. ONE transition function = `db_node_machine.node_transition`. NO permanent alias-shim — consumers adopt `state`/`title`/`last_active_at` directly.
- **`background` is a first-class node state (R2-1, baked decision):** the `status↔state` value map is BIJECTIVE over the live vocab — `active↔open`, `background↔background`, `running↔running`, `closed↔done`, `archived↔archived`. `background` is NOT collapsed into `running` (the collapse is lossy and breaks the watchdog reaper at `watchdog.py:875` plus the two adjacent cockpit panels 2a Running / 2b Background). `db_node_machine` gains `('background', …)` transitions so the single machine accepts the state without raising. Separating focus (foreground/background) from lifecycle into its own dimension is a FUTURE refinement, explicitly OUT of P8 scope.
- **Cockpit changes:** after any cockpit layout/read change, run `uv run src/juggle_cli.py cockpit --smoke --all-viewports` and paste the summary as evidence.

---

## Decisions baked in (do NOT re-open)

| # | Decision | Source |
|---|----------|--------|
| Q1 | Rename the ~107 `row['status']`/`['topic']`/`['last_active']` + value-compare consumers to `state`/`title`/`last_active_at`; **delete the alias-shim** (`CONV_ALIAS_SHIM`, `STATE_AS_STATUS_SQL`). No permanent shim. | DA H2 |
| Q2 | Model the task→dispatch-thread relation **explicitly** (typed `node_edges` edge-kind **or** `agents.assigned_node`), not a bare nullable column. (Final shape → OQ2.) | DA M1 |
| Q3 | ONE task-entry vocab = `'open'`. Rename `db_graph`/`db_topics` `pending→open` everywhere; **delete** `backfill_graph_parity`'s `open→pending` correction; delete `'pending'`. | DA C3 |
| Q4 | Delete dead `juggle_migrate_lifecycle.py` in the terminal-drop task. | DA L1 |
| Engine | ONE engine: `db_graph.task_transition` / `db_topics.topic_transition` delegate to `db_node_machine.node_transition`; `nodes` is authoritative; `add_node` computes readiness via the unified machine. | DA C1 |
| Table | Keep ONE wide `nodes` table (do NOT split per-kind); ADD `CHECK` constraints / a guard to enforce the kind discriminator. | DA M2 |
| BG | `background` stays a FIRST-CLASS node state — **bijective** map (`active↔open, background↔background, running↔running, closed↔done, archived↔archived`); `db_node_machine` gains `('background', …)` transitions; conversation background-ness is READ from `nodes.state='background'`, NOT derived from the agent binding (deriving is the FUTURE focus/lifecycle split, out of P8 scope). | DA-R2 R2-1 |

---

## Step → Finding → Counter map (how each step is monotonic + agent-verified)

Each step drives a NAMED counter strictly toward its floor; the suite stays green at every commit. The composite "legacy surface" never increases.

| Step | Findings | Monotonic counter (agent-verifiable) | Floor |
|------|----------|--------------------------------------|-------|
| 1 | C3, C1, **R2-2, R2-4** | `grep -rnE "'pending'" src/ --include='*.py'` (live) **and** count of transition tables (`_TRANSITIONS` deleted). R2-2: engine-delegation is ONE atomic commit (no dangling import). R2-4: M51 fail-LOUD. | pending→0 live; 1 machine |
| 2 | H1, **R2-1** | `grep -rn '"active": "open"' src/` (duplicate forward-maps) **and** `background` is bijective: `state_for_status('background')=='background'` AND `status_for_state('background')=='background'` (round-trips losslessly) | 3 → 1 maps; background bijective |
| 3 | C2 (conv), H2, **R2-1** | `doctor --pre-p8-check --json` `.static.fail` (conversation-cluster legacy refs cut, **incl. watchdog/dispatch_core/cmd_context/context_startup**) + shim deleted; reaper + cockpit 2a/2b counts unchanged pre/post flip | strictly ↓ |
| 4 | C2 (graph), C1 (write), **R2-3, R2-5** | `doctor --pre-p8-check --json` `.static.fail` → **0**; `INSERT INTO graph_*` in `add_node` = 0; `grep -rl db_mirror src/` → empty (R2-5); pre-flip backup artifact present + reverse-backfill documented (R2-3) | static.fail → 0 |
| 5 | H4, M4 | `CREATE_NODES` column count (+4); gate's `excluded_files` + `import_refs` reported | DDL complete; gate honest |
| 6 | M1, M2, M3, L1, H5 | legacy tables present (`p8_drop_ready` → `already-dropped`); `test -f juggle_migrate_lifecycle.py` false; spec no longer `LOCKED`-stale | 0 legacy tables |

Baseline to record before Step 1 (run from worktree root):
```bash
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run python src/juggle_cli.py init-db   # stand up a tmp DB for the gate
uv run python src/juggle_cli.py doctor --pre-p8-check --json | python3 -c 'import sys,json;d=json.load(sys.stdin);print("static.fail=",d["static"]["fail"])'
```

---

## File Structure (what changes, and its single responsibility)

**Engine / vocab (Steps 1–2):**
- `src/dbops/db_node_machine.py` — the SOLE transition logic. Gains `'open'` already; loses its duplicate `_THREAD_STATUS_TO_NODE_STATE` (imports from `node_translation`).
- `src/dbops/db_graph.py` — task-node DB wrapper. `task_transition` delegates to `node_transition`; `_TRANSITIONS` DELETED; writes `nodes` (lockstep-mirrors `graph_tasks` until Step 4). Vocab `open`.
- `src/dbops/db_topics.py` — topic-tier DB wrapper. `topic_transition` delegates to `node_transition` (stops importing `db_graph._TRANSITIONS`). Vocab `open`.
- `src/dbops/db_graph_marking.py` — completion/failure mapping. `_ADVANCE_*` dicts re-keyed `pending→open`.
- `src/dbops/node_translation.py` — the ONE vocab module (forward + reverse + generated SQL). Shim deleted in Step 3.
- `src/dbops/migrations_nodes.py` — `_THREAD_STATUS_MAP` deleted (import from `node_translation`); `_task_state` becomes a no-op once vocab unified.
- `src/dbops/migration_nodes_parity.py` — `backfill_graph_parity`'s two `open→pending` UPDATEs DELETED (keep `dispatch_thread_id` backfill).
- `src/dbops/schema_graph.py` — `graph_tasks`/`graph_topics` `DEFAULT 'pending'` → `'open'`.
- `src/dbops/state_write.py` — **NEW** (Step 1): single in-transaction state-writer helper used by every task/topic state CAS, so `nodes`.state never drifts (fixes M3 early). Drops the `graph_*` half in Step 4.
- `src/dbops/migration_51_state_vocab.py` — **NEW** (Step 1): idempotent `pending→open` data migration for existing DBs.

**Cluster flips (Steps 3–4):**
- `src/juggle_cockpit_model.py`, `src/dbops/threads.py`, `src/juggle_cmd_threads.py`, `src/juggle_cmd_projects.py`, `src/juggle_cmd_agents_lifecycle.py`, `src/juggle_cmd_runs.py`, `src/juggle_cmd_selfheal.py`, `src/juggle_project_summary.py` — conversation consumers (Step 3).
- `src/juggle_cockpit_graph_dag.py`, `src/dbops/orphan_guard.py`, `src/dbops/db_topics_reconcile.py`, `src/juggle_graph_*.py`, `src/juggle_add_node.py` — graph consumers + write-cut (Step 4).
- `src/dbops/db_mirror.py` — DELETED (Step 4; mirror concept dead).

**Honesty + cleanup (Steps 5–6):**
- `src/dbops/schema_nodes.py` — fold 4 parity columns into `CREATE_NODES` (Step 5).
- `src/dbops/p8_readiness.py` — honest Gate A (import-reachability + excluded-files log) (Step 5).
- `src/dbops/migration_52_dispatch_edge.py`, CHECK-constraint guard, `migration_53_p8_drop.py` — Step 6.
- `src/juggle_migrate_lifecycle.py` — DELETED (Step 6).
- `specs/2026-06-18-unified-topic-graph.md` — demoted to "superseded" + as-built addendum (Step 6).

---

# STEP 1 — One vocab (`open`) + one transition engine [C3, C1]

**Outcome:** `'pending'` is gone from live code; `db_node_machine.node_transition` is the SOLE transition logic (all of `db_graph.task_transition`, `db_topics.topic_transition`, and the `db_graph_marking` walkers delegate to it); `nodes.state` is written in lockstep with every task/topic state change (M3 fixed early); existing DBs carrying `pending` are migrated to `open`. Dual-write to `graph_*` stays ON (cut in Step 4) — this step changes LOGIC and VOCAB only, so reads remain valid and the suite stays green.

**Why this ordering is green:** delegating the transition decision and renaming the vocab does not move any read source. The only data hazard is existing rows storing `'pending'`; Task 1.1 lands the migration FIRST so the renamed engine never queries a `'pending'` row that the code no longer understands.

**Step-1 monotonic gate (agent runs, no human):**
```bash
# pending eliminated from live code (excludes the dated historical migration 44 + the new 51):
grep -rnE "'pending'" src/ --include='*.py' | grep -vE 'migrations_nodes\.py|migration_51_state_vocab\.py' ; echo "exit=$?"   # expect: no matches
# exactly one transition table remains:
grep -rn "_TRANSITIONS" src/ --include='*.py'   # expect: only references to db_node_machine internals, db_graph._TRANSITIONS gone
```

---

### Task 1.1 — Migration 51: `pending → open` for existing DBs

**Files:**
- Create: `src/dbops/migration_51_state_vocab.py`
- Modify: `src/dbops/migrations_recent.py` (wire after 50, `:328`)
- Test: `tests/test_migration_51_state_vocab.py`

**Interfaces:**
- Produces: `migrate_51_state_vocab(conn: sqlite3.Connection) -> None` — idempotent; maps `graph_tasks.state`, `graph_topics.state`, and `nodes.state` `'pending' → 'open'` for `kind='task'` rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_51_state_vocab.py
import sqlite3
from dbops.migration_51_state_vocab import migrate_51_state_vocab

def _mk(conn):
    conn.execute("CREATE TABLE graph_tasks (id TEXT, state TEXT)")
    conn.execute("CREATE TABLE graph_topics (id TEXT, state TEXT, is_mirror INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE nodes (id TEXT, kind TEXT, parent_id TEXT, state TEXT)")
    conn.execute("INSERT INTO graph_tasks VALUES ('t1','pending'),('t2','ready')")
    conn.execute("INSERT INTO graph_topics VALUES ('p1','pending',0)")
    conn.execute("INSERT INTO nodes VALUES ('t1','task',NULL,'pending'),('c1','conversation',NULL,'open')")
    conn.commit()   # R2-4: migrate now uses BEGIN IMMEDIATE — setup must be committed first

def test_migration_51_maps_pending_to_open():
    """2026-06-27 P8 C3: existing DBs store task state 'pending'; the unified
    engine only understands 'open'. Migration 51 must rewrite pending→open so
    the renamed engine never queries an un-modelled state."""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_51_state_vocab(conn)
    assert conn.execute("SELECT state FROM graph_tasks WHERE id='t1'").fetchone()[0] == "open"
    assert conn.execute("SELECT state FROM graph_topics WHERE id='p1'").fetchone()[0] == "open"
    assert conn.execute("SELECT state FROM nodes WHERE id='t1'").fetchone()[0] == "open"
    # conversation node untouched (its 'open' is the conversation entry state):
    assert conn.execute("SELECT state FROM nodes WHERE id='c1'").fetchone()[0] == "open"

def test_migration_51_idempotent():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_51_state_vocab(conn); migrate_51_state_vocab(conn)   # second run is a no-op
    assert conn.execute("SELECT COUNT(*) FROM graph_tasks WHERE state='pending'").fetchone()[0] == 0


def test_migration_51_fail_loud_on_lock(tmp_path):
    """2026-06-27 P8 R2-4: M51 must FAIL-LOUD (propagate) on write-lock contention,
    never silently skip — a swallowed skip strands 'pending' rows the renamed engine
    cannot process. RED on the v1 fail-soft code (it returns without raising)."""
    import pytest
    dbf = str(tmp_path / "m51.db")
    setup = sqlite3.connect(dbf)
    setup.execute("CREATE TABLE graph_tasks (id TEXT, state TEXT)")
    setup.execute("INSERT INTO graph_tasks VALUES ('t1','pending')")
    setup.commit()
    holder = sqlite3.connect(dbf, timeout=0); holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")                 # hold the write lock
    victim = sqlite3.connect(dbf, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_51_state_vocab(victim)            # must RAISE, not swallow
    finally:
        holder.execute("ROLLBACK")
    # the still-pending row proves the (failed) migration did NOT partially commit:
    assert setup.execute("SELECT state FROM graph_tasks WHERE id='t1'").fetchone()[0] == "pending"
```

- [ ] **Step 2: Run test to verify it fails** — `uv run pytest tests/test_migration_51_state_vocab.py -q` → FAIL (module missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/dbops/migration_51_state_vocab.py
"""Migration 51 (P8 C3 + R2-4): unify the task-entry vocab to 'open' — FAIL-LOUD.

Rewrites the legacy 'pending' task/topic state to 'open' in graph_tasks,
graph_topics, and the mirrored task nodes, so the unified node_transition
engine (which only models 'open') never meets a 'pending' row. Idempotent
(WHERE state='pending' -> second run no-ops); value-only (no schema change).

FAIL-LOUD (R2-4): the SAME-RELEASE engine rename (Tasks 1.3+1.4) hard-depends on
this migration having applied. A fail-soft swallow would silently strand
'pending' rows the renamed engine can neither see (ready_eligible) nor transition
(node_transition has no 'pending' entry) -> tasks stall with no error. So we take
the write lock up front with BEGIN IMMEDIATE (exactly like Migration 45,
migration_selfheal_status_check.py:38-69) and let lock contention PROPAGATE; the
init_db caller aborts the upgrade on the raise. Apply via juggle doctor (behind
assert_migration_allowed); never run directly against the shared prod DB.
"""
from __future__ import annotations
import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_51_state_vocab(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # Pre-Migration-44 DB without any target table -> return BEFORE taking the
    # lock so a brand-new DB upgrade is a cheap no-op (not a spurious lock grab).
    if not ({"graph_tasks", "graph_topics", "nodes"} & tables):
        return
    prev_isolation = conn.isolation_level
    conn.isolation_level = None              # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")          # write lock up front; raises on contention (fail-LOUD)
    try:
        if "graph_tasks" in tables:
            conn.execute("UPDATE graph_tasks SET state='open' WHERE state='pending'")
        if "graph_topics" in tables:
            conn.execute("UPDATE graph_topics SET state='open' WHERE state='pending'")
        if "nodes" in tables:
            conn.execute(
                "UPDATE nodes SET state='open' WHERE kind='task' AND state='pending'")
        conn.execute("COMMIT")
        _log.info("Migration 51: task-state vocab unified pending->open")
    except Exception:
        conn.execute("ROLLBACK")             # fail-LOUD: abort the upgrade, do NOT swallow
        raise
    finally:
        conn.isolation_level = prev_isolation
```

Wire it (`migrations_recent.py`, immediately after the migration-50 block at `:328`):
```python
    from dbops.migration_51_state_vocab import migrate_51_state_vocab
    migrate_51_state_vocab(conn)   # P8 C3: pending->open (runs before vocab-renamed engine)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_migration_51_state_vocab.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "p8(vocab): Migration 51 — unify task-state pending->open for existing DBs [C3]"`

**Acceptance gate (agent):** `uv run python src/juggle_cli.py doctor --dry-run` succeeds against a tmp DB seeded with `pending` rows, and a follow-up `SELECT DISTINCT state FROM graph_tasks` returns no `pending`.

> **Note (R2-6, accepted — no action):** Migration 44 (`migrations_nodes.py`) backfills task nodes with `'pending'`, and M51 then rewrites them to `'open'` on every `init_db` (both idempotent/presence-guarded). This is a permanent double value-migration and the reason the Step-1 `'pending'` grep must keep excluding `migrations_nodes.py`. It is HARMLESS (M51's `WHERE state='pending'` no-ops once converged) and not worth the risk of editing M44's existing pins. Accepted as-is; revisit only if M44's pins later need touching for another reason.

---

### Task 1.2 — Delete `backfill_graph_parity`'s `open→pending` correction

**Files:**
- Modify: `src/dbops/migration_nodes_parity.py:97-101,110-115` (delete the two `UPDATE nodes SET state='pending'` blocks; KEEP both `dispatch_thread_id` backfills)
- Test: `tests/test_migration_nodes_parity.py` (update the existing pending-assertion to assert `open`)

- [ ] **Step 1: Update the failing pin** — in `test_migration_nodes_parity.py`, find the assertion that a task node ends `state='pending'` and flip it to assert `state='open'`; add a docstring line: `# 2026-06-27 P8 C3: parity backfill must NOT re-introduce 'pending'`.
- [ ] **Step 2: Run** → FAIL (current code still rewrites to `pending`).
- [ ] **Step 3: Edit** — delete lines `97-101` (the `graph_tasks`-driven `state='pending'`) and `110-115` (the `graph_topics`-driven `state='pending'`) in `backfill_graph_parity`. The function now backfills ONLY `dispatch_thread_id`. Update its docstring to drop the "pending-state corrected" clause.
- [ ] **Step 4: Run** `uv run pytest tests/test_migration_nodes_parity.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "p8(vocab): drop backfill open->pending correction [C3]"`

**Acceptance gate (agent):** `grep -nE "state='pending'" src/dbops/migration_nodes_parity.py` → empty.

---

### Task 1.3 — Rename `db_graph` vocab `pending → open` and delegate `task_transition` to `node_transition`

> **⚠️ ATOMIC COMMIT (R2-2): Tasks 1.3 and 1.4 land as ONE git commit.** `db_topics.py:15` does `from dbops.db_graph import _EVENTS, _TRANSITIONS, _cx` at **module-load time** (used at `:35` `_EVENTS`, `:41,46` `_TRANSITIONS`, and `_cx` at `:64,74,83,128,235`). Deleting `_TRANSITIONS`/`_EVENTS` from `db_graph` (this task) WITHOUT fixing that importer (Task 1.4) leaves `import dbops.db_topics` raising `ImportError` at the in-between commit — every test that transitively imports it is collect-error RED, violating green-per-commit. So **do NOT commit at the end of 1.3**; implement 1.3 + 1.4 together and commit once at the end of 1.4. **`_cx` must REMAIN exported by `db_graph`** (db_topics still imports it) — delete ONLY `_TRANSITIONS` and `_EVENTS`. The "`_TRANSITIONS` greppably gone" acceptance check moves to the end of Task 1.4. (This supersedes v1's D7 "transitional re-export" suggestion, which contradicted 1.3's own acceptance gate.)

**Files:**
- Modify: `src/dbops/db_graph.py` (`VALID_STATES`, `create_task`, `ready_eligible`, `recompute_ready`, `task_transition`; DELETE `_TRANSITIONS` + `_EVENTS`)
- Modify: `src/dbops/db_node_machine.py` (export the legal-event set for `db_graph`/`db_topics` to validate against)
- Modify: `src/dbops/schema_graph.py:20,43` (`DEFAULT 'pending'` → `DEFAULT 'open'`)
- Test: `tests/test_db_graph.py`, `tests/test_node_transition.py`

**Interfaces:**
- Consumes: `db_node_machine.node_transition(state, event, kind) -> str`, `db_node_machine._KIND_LEGAL`.
- Produces: `db_graph.task_transition(db, task_id, event, conn=None) -> str` (unchanged signature; now delegates the decision to `node_transition` with `kind='task'`, writes `nodes` + (compat) `graph_tasks` via `state_write`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_graph.py  (add)
def test_task_transition_delegates_to_node_machine(tmp_db):
    """2026-06-27 P8 C1: task_transition must compute next-state via the unified
    node_transition (single source of transition logic), using 'open' vocab."""
    from dbops import db_graph
    from juggle_add_node import add_node
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")
    assert r["state"] == "open"                      # NOT 'pending'
    db_graph.task_transition(tmp_db, r["node_id"], "deps_ready")
    assert db_graph.get_task(tmp_db, r["node_id"])["state"] == "ready"

def test_db_graph_has_no_local_transition_table():
    import dbops.db_graph as g
    assert not hasattr(g, "_TRANSITIONS"), "duplicate transition table must be deleted"
```
(`tmp_db` fixture: per the repo's `tests/conftest.py` redirect; if no shared fixture exists, build a `JuggleDB(tmp_path)` and `init_db()` — see existing `test_add_node.py` for the pattern.)

- [ ] **Step 2: Run** → FAIL (`add_node` still returns 'open' for nodes but get_task reads 'pending'; `_TRANSITIONS` still present).
- [ ] **Step 3: Implement.**
  - In `db_node_machine.py`, add a helper the legacy wrappers can reuse for validation:
    ```python
    def legal_events(kind: str) -> frozenset[str]:
        """Events legal for a kind (raises InvalidTransition on unknown kind)."""
        legal = _KIND_LEGAL.get(kind)
        if legal is None:
            raise InvalidTransition(f"unknown node kind: {kind!r}")
        return legal
    ```
  - In `db_graph.py`:
    - DELETE `VALID_STATES`'s `"pending"` membership and replace the set with the node-machine states, or import the canonical set. Replace `_TRANSITIONS`/`_EVENTS` definitions with: `from dbops.db_node_machine import node_transition, InvalidTransition, legal_events`.
    - Rewrite `task_transition` to read current state, call `node_transition(state, event, "task")`, and write via the lockstep writer (Task 1.5 introduces `state_write`; until then write `nodes` + `graph_tasks` inline in one `_cx`):
      ```python
      def task_transition(db, task_id, event, conn=None):
          if event not in legal_events("task"):
              raise ValueError(f"graph task event unknown: {event!r}")
          task = get_task(db, task_id, conn=conn)
          if task is None:
              raise ValueError(f"graph task not found: {task_id!r}")
          try:
              new_state = node_transition(task["state"], event, "task")
          except InvalidTransition as e:
              raise ValueError(str(e)) from e
          # ... build sets (state, updated_at, verified_at on 'verified',
          #     thread_id=NULL on 'reload') and write nodes + graph_tasks (compat)
      ```
    - `create_task` INSERT `'pending'` → `'open'`.
    - `ready_eligible` / `recompute_ready` `state='pending'` → `state='open'`.
  - `schema_graph.py:20,43` `DEFAULT 'pending'` → `DEFAULT 'open'`.
- [ ] **Step 4: Run** `uv run pytest tests/test_db_graph.py tests/test_node_transition.py tests/test_graph_marking.py -q` → PASS.
- [ ] **Step 5: DO NOT COMMIT (R2-2).** Leave the working tree dirty and proceed straight to Task 1.4 — 1.3 + 1.4 are one atomic commit (landed at the end of 1.4). Run `uv run pytest tests/test_db_graph.py tests/test_node_transition.py tests/test_graph_marking.py -q` to confirm the db_graph half is internally consistent, but expect `import dbops.db_topics` to still be broken until 1.4 fixes its importer.

**Acceptance gate (agent, checked at the END of Task 1.4 — the shared commit):** `grep -rn "_TRANSITIONS\b" src/dbops/db_graph.py` → empty (only `_NODE_TRANSITIONS` in db_node_machine remains); `grep -n "'pending'" src/dbops/db_graph.py src/dbops/schema_graph.py` → empty; **`grep -n "def _cx\|_cx" src/dbops/db_graph.py` still returns `_cx` (kept for db_topics)**; `python -c "import dbops.db_topics"` exits 0.

---

### Task 1.4 — Rename `db_topics` vocab + delegate `topic_transition`; fix `db_graph_marking` walkers

**Files:**
- Modify: `src/dbops/db_topics.py:15` — change `from dbops.db_graph import _EVENTS, _TRANSITIONS, _cx` to `from dbops.db_graph import _cx` (KEEP `_cx`; drop `_EVENTS`/`_TRANSITIONS`) and add `from dbops.db_node_machine import node_transition, InvalidTransition, legal_events`; `:33-46` (`topic_transition` delegates to `node_transition` with `kind='task'`, replacing the `_EVENTS`/`_TRANSITIONS` lookups), `:76` (INSERT `'pending'`→`'open'`), `:197` (`_DISPATCHABLE_TASK_STATES`), `:214,238` (`state='pending'`), `:253,297,329` (`_ADVANCE_*` / propagate `pending`→`open`)
- Modify: `src/dbops/db_graph_marking.py:19,61` (`_ADVANCE_TO_INTEGRATING`/`_ADVANCE_TO_RUNNING` keys `"pending"`→`"open"`), `:119,156` (`("blocked-failed","pending")`/`("pending","ready")` membership → `"open"`)
- Test: `tests/test_graph_marking.py`, `tests/test_graph_spec_topics.py`, `tests/test_graph_reconcile.py`

- [ ] **Step 1: Write the failing test** — add to `test_graph_marking.py`:
```python
def test_marking_walks_from_open_not_pending(tmp_db):
    """2026-06-27 P8 C3: completion-marking walkers must key on 'open' (the
    unified entry state), not the deleted 'pending'."""
    from dbops import db_graph_marking as m
    assert "open" in m._ADVANCE_TO_INTEGRATING and "pending" not in m._ADVANCE_TO_INTEGRATING
    assert "open" in m._ADVANCE_TO_RUNNING and "pending" not in m._ADVANCE_TO_RUNNING
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the renames above. `db_topics.topic_transition` mirrors the `db_graph.task_transition` delegation (kind `"task"`; topics are `kind='task' AND parent_id IS NULL`). Keep `db_topics` writing `graph_topics` + lockstep `nodes` (Task 1.5).
- [ ] **Step 4: Run** `uv run pytest tests/test_graph_marking.py tests/test_graph_spec_topics.py tests/test_graph_reconcile.py tests/test_db_mirror.py -q` → PASS, then FULL `make test`.
- [ ] **Step 5: Commit BOTH 1.3 + 1.4 as ONE commit (R2-2)** — `git add -A && git commit -m "p8(engine): db_graph+db_topics+marking delegate to node_transition; vocab open [C1,C3,R2-2]"`. Before committing, run `uv run pytest -q --co 2>&1 | grep -i "error" ; echo "collect-exit=$?"` → no import/collection errors, then FULL `make test` green.

**Acceptance gate (agent):** `grep -rnE "'pending'" src/dbops/db_topics.py src/dbops/db_graph_marking.py` → empty; `grep -n "_TRANSITIONS" src/dbops/db_topics.py` → empty.

---

### Task 1.5 — Single in-transaction state-writer (`nodes` authoritative; M3 fixed early)

**Files:**
- Create: `src/dbops/state_write.py`
- Modify: `src/dbops/db_graph.py`, `src/dbops/db_topics.py` (route every `state=` UPDATE/CAS through it), `src/juggle_add_node.py:258-274` (compute readiness in-transaction; no post-commit mirror)
- Modify: `src/juggle_graph_dispatch.py:54` (`claim_task` CAS), `src/juggle_graph_dispatch.py` `sweep_stale_claims` — route through the helper
- Test: `tests/test_add_node.py`, `tests/test_graph_dispatch.py`

**Interfaces:**
- Produces:
  - `write_state(conn, node_id, new_state, *, now, extra=None) -> None` — writes `nodes` AND (compat, until Step 4) `graph_tasks`/`graph_topics` in the caller's transaction. `extra` carries `verified_at`/`thread_id=NULL`.
  - `cas_state(conn, node_id, *, frm, to, now) -> int` — conditional `UPDATE … WHERE state=:frm` on `nodes` (authoritative; returns rowcount as the claim token) + mirror to the legacy row. Used by `recompute_ready`, `claim_task`, `sweep_stale_claims`.

- [ ] **Step 1: Write the failing test** — the M3 pin:
```python
# tests/test_add_node.py  (add)
def test_add_node_task_state_atomic_no_drift(tmp_db):
    """2026-06-27 P8 M3: a no-dep task's nodes.state must equal graph_tasks.state
    immediately after add_node returns (computed in-transaction, no post-commit
    mirror window)."""
    from dbops import db_graph
    from juggle_add_node import add_node
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")  # no deps → ready
    node = tmp_db._connect().execute("SELECT state FROM nodes WHERE id=?", (r["node_id"],)).fetchone()
    assert node[0] == db_graph.get_task(tmp_db, r["node_id"])["state"] == "ready"
```
- [ ] **Step 2: Run** → FAIL (current `add_node` mirrors post-commit, and the readiness path is legacy-only).
- [ ] **Step 3: Implement** `state_write.py`; route `db_graph.task_transition`, `db_topics.topic_transition`, `recompute_ready`, `claim_task`, `sweep_stale_claims`, and `add_node`'s readiness through it. In `add_node._add_task_node`, compute readiness inside the existing `conn` transaction (deps already loaded) and `write_state(conn, node_id, "ready"|"open", ...)` BEFORE `conn.commit()`; DELETE the post-commit `db_graph.recompute_ready` + `_update_node_state` mirror block (`:258-274`). Keep the dual-write of `graph_tasks`/`graph_edges` (Step 4 removes it).
- [ ] **Step 4: Run** `uv run pytest tests/test_add_node.py tests/test_graph_dispatch.py tests/test_dispatch_node.py -q` → PASS, then FULL `make test`.
- [ ] **Step 5: Commit** — `git commit -m "p8(engine): single in-txn state writer; nodes authoritative; fix M3 drift [C1,M3]"`

**Acceptance gate (agent):** the M3 pin passes; `grep -n "recompute_ready\|_update_node_state" src/juggle_add_node.py` → empty (readiness now in-transaction).

**Step-1 DONE when:** `make test` green AND `grep -rnE "'pending'" src/ --include='*.py' | grep -vE 'migrations_nodes\.py|migration_51_state_vocab\.py'` → empty AND `grep -rn "node_transition(" src/ | grep -vE "def node_transition|noqa"` → **>0 real call sites** (C1 Agent-verify, partial — full "graph_tasks never written" lands in Step 4).

---

# STEP 2 — Centralize the vocab maps + make `background` first-class [H1, R2-1]

**Outcome:** ONE module owns the `status↔state` value map (both directions); `db_node_machine`, `migrations_nodes` import it; the SQL `CASE` is GENERATED from the dict (no hand-synced second encoding); the divergent `db_mirror._THREAD_TO_STATE` is flagged for elimination in Step 4 (it is a semantically distinct thread-status→TOPIC-state map and dies with the module). **AND (R2-1):** `background` becomes a first-class node state so the `status↔state` map is BIJECTIVE over the live vocab (`background↔background`, not the lossy `background→running`), and `db_node_machine` is taught the `('background', …)` transitions so the single machine accepts it. This MUST land before Step 3 (the conversation flip reads `nodes.state` for background-ness).

**Step-2 monotonic gate:** `grep -rn '"active": "open"' src/ --include='*.py'` → **1** (was 3); AND `background` round-trips losslessly — `state_for_status('background')=='background'` and `status_for_state('background')=='background'`; AND `node_transition('background','archive','conversation')` does not raise.

---

### Task 2.0 — Add `background` as a first-class bijective node state [R2-1]

**Files:**
- Modify: `src/dbops/node_translation.py:5-9` (`STATUS_TO_STATE`: `"background": "running"` → `"background": "background"`), `:20-21` (`STATE_TO_STATUS`: add `"background": "background"`), `:16-19` (update the "bijective" comment to INCLUDE `background`)
- Modify: `src/dbops/db_node_machine.py` (add the `('background', …)` transitions + the `('open','dispatch_bg')` entry to `_NODE_TRANSITIONS`; extend the `conversation` set in `_KIND_LEGAL`; set `_THREAD_STATUS_TO_NODE_STATE["background"] = "background"` for interim consistency until Task 2.2 re-derives it)
- Test (REWRITE the two existing pins — see authorization note): `tests/test_node_translation.py` (`test_status_to_state_full_map`, `test_state_for_status_known_values`), `tests/test_node_transition.py` (the `("background", "running")` parametrize row)
- Test (new pins): `tests/test_node_translation.py`, `tests/test_node_transition.py`

**Interfaces:**
- Produces: `state_for_status('background')=='background'`, `status_for_state('background')=='background'`; `node_transition('background', <legal event>, 'conversation')` no longer raises.

> **Pin-rewrite authorization (R2-1):** `test_node_translation.py:8` (`test_status_to_state_full_map`), `:23` (`test_state_for_status_known_values`), and `test_node_transition.py:164` (the `("background","running")` parametrize row) currently pin the OLD lossy `background→running`. R2-1 is the user-sanctioned decision to change that behavior, so these pins are **REWRITTEN in place** (not deleted) to assert the new bijective `background→background` — same seam, corrected value (satisfies the regression-pin gate). **Do NOT touch** `test_data_migration.py:50 test_background_maps_to_running` — it pins the SEPARATE, dying `juggle_migrate_lifecycle` migration (threads-level legacy collapse) and is removed *with that module* in Task 6.3.

- [ ] **Step 1: Write/rewrite the failing tests**

```python
# tests/test_node_translation.py
#  (a) REWRITE test_status_to_state_full_map's expected dict: "background": "background"
#  (b) REWRITE test_state_for_status_known_values: state_for_status("background") == "background"
#  (c) ADD the bijection pin:
def test_status_state_bijective_over_live_vocab():
    """2026-06-27 P8 R2-1: status<->state is bijective over the LIVE vocab; a
    'background' conversation round-trips losslessly (it was collapsed to 'running',
    which broke the watchdog reaper + the two distinct cockpit panels)."""
    from dbops.node_translation import STATUS_TO_STATE, STATE_TO_STATUS
    for status in ("active", "background", "running", "closed", "archived"):
        state = STATUS_TO_STATE[status]
        assert STATE_TO_STATUS[state] == status, f"{status!r} not invertible (state={state!r})"
```

```python
# tests/test_node_transition.py
#  (d) REWRITE the parametrize row ("background", "running") -> ("background", "background")
#  (e) ADD the machine-accepts-background pin:
def test_background_state_accepted_by_machine():
    """2026-06-27 P8 R2-1: the unified machine must accept the 'background' state for
    conversation nodes — it must NOT raise InvalidTransition on a live state (the C3
    failure mode, now for background)."""
    from dbops.db_node_machine import node_transition
    assert node_transition("open", "dispatch_bg", "conversation") == "background"
    assert node_transition("background", "foreground", "conversation") == "open"
    assert node_transition("background", "archive", "conversation") == "archived"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_node_translation.py tests/test_node_transition.py -q` → FAIL (map still collapses background→running; machine has no `('background', …)` key).

- [ ] **Step 3: Implement**

`node_translation.py`:
```python
STATUS_TO_STATE = {
    "active": "open", "closed": "done", "background": "background",
    "running": "running", "failed": "failed-exec", "done": "done",
    "archived": "archived",
}
# Reverse value-map state -> status. Bijective over the LIVE vocab
# {active,background,running,closed,archived} <-> {open,background,running,done,archived};
# legacy-only inputs (failed, done) are non-live pass-throughs (set_thread_status cannot
# emit them) and are out of the bijective set.
STATE_TO_STATUS = {"open": "active", "background": "background", "running": "running",
                   "done": "closed", "archived": "archived"}
```

`db_node_machine.py` — add to `_NODE_TRANSITIONS` (after the `done` block):
```python
    # background — conversation agent dispatched in the background (R2-1).
    # Kept DISTINCT from 'running' so the watchdog reaper + cockpit 2a/2b stay correct.
    ("open",       "dispatch_bg"): "background",
    ("background", "foreground"):  "open",       # agent completes / user resumes -> active
    ("background", "answer"):      "done",        # bg agent answered/closed the conversation
    ("background", "archive"):     "archived",
```
extend the conversation legal set:
```python
    "conversation": frozenset({"answer", "archive", "dispatch_bg", "foreground"}),
```
and `_THREAD_STATUS_TO_NODE_STATE["background"] = "background"` (interim; Task 2.2 deletes this dict and re-derives `thread_status_to_node_state` from `STATUS_TO_STATE`, which now yields `background`).

- [ ] **Step 4: Run** `uv run pytest tests/test_node_translation.py tests/test_node_transition.py -q` → PASS, then FULL `make test` (a `conv_node_mirror`-written background thread now mirrors to `nodes.state='background'`; confirm no test asserted the old `'running'` mirror value — there is none in the suite, but run full to be sure).
- [ ] **Step 5: Commit** — `git commit -m "p8(state): background is a first-class bijective node state [R2-1]"`

**Acceptance gate (agent):**
```bash
python -c "from dbops.node_translation import STATUS_TO_STATE as f, STATE_TO_STATUS as r; assert all(r[f[s]]==s for s in ('active','background','running','closed','archived')), 'not bijective'; print('bijective OK')"
python -c "from dbops.db_node_machine import node_transition; node_transition('background','archive','conversation'); node_transition('open','dispatch_bg','conversation'); print('machine accepts background')"
```

---

### Task 2.1 — Make `node_translation` own both directions + generate the SQL

**Files:**
- Modify: `src/dbops/node_translation.py` (generate `STATE_AS_STATUS_SQL` from `STATE_TO_STATUS`; add a parity self-check helper)
- Test: `tests/test_node_translation.py`

- [ ] **Step 1: Write the failing test** — the SQL/dict equivalence pin:
```python
# tests/test_node_translation.py  (add)
import sqlite3
from dbops import node_translation as nt

def test_state_as_status_sql_matches_dict():
    """2026-06-27 P8 H1: the SQL CASE must be generated from STATE_TO_STATUS so
    the two encodings can never diverge."""
    conn = sqlite3.connect(":memory:")
    for state, expected in nt.STATE_TO_STATUS.items():
        got = conn.execute(
            f"SELECT {nt.STATE_AS_STATUS_SQL} FROM (SELECT ? AS state)", (state,)
        ).fetchone()[0]
        assert got == expected, f"{state}: SQL={got} dict={expected}"
```
- [ ] **Step 2: Run** → FAIL (current `STATE_AS_STATUS_SQL` is a hand-written literal; `done`→`closed` etc. may diverge from the dict if edited).
- [ ] **Step 3: Implement** — generate the CASE:
```python
def _build_state_as_status_sql() -> str:
    whens = " ".join(f"WHEN '{s}' THEN '{st}'" for s, st in STATE_TO_STATUS.items())
    return f"CASE state {whens} ELSE state END AS status"

STATE_AS_STATUS_SQL = _build_state_as_status_sql()
```
(Keep `CONV_ALIAS_SHIM` for now; Step 3 deletes it.)
- [ ] **Step 4: Run** `uv run pytest tests/test_node_translation.py -q` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "p8(maps): generate STATE_AS_STATUS_SQL from dict + parity pin [H1]"`

---

### Task 2.2 — Delete the duplicate forward-maps; import the canonical one

**Files:**
- Modify: `src/dbops/db_node_machine.py:111-127` (delete `_THREAD_STATUS_TO_NODE_STATE`; re-implement `thread_status_to_node_state` over `node_translation.STATUS_TO_STATE`)
- Modify: `src/dbops/migrations_nodes.py:22-30` (delete `_THREAD_STATUS_MAP`; import `STATUS_TO_STATE`)
- Test: `tests/test_node_transition.py`, `tests/test_nodes_schema_migration.py`

- [ ] **Step 1: Write the failing test**
```python
def test_no_duplicate_forward_map():
    """2026-06-27 P8 H1: the thread-status->node-state map exists exactly once."""
    import subprocess
    n = subprocess.run(["grep","-rn",'"active": "open"',"src/"],
                       capture_output=True,text=True).stdout.strip().splitlines()
    assert len(n) == 1, f"expected 1 forward-map definition, found {len(n)}:\n{n}"
```
- [ ] **Step 2: Run** → FAIL (3 copies).
- [ ] **Step 3: Implement** — `db_node_machine.thread_status_to_node_state` becomes `return STATUS_TO_STATE[status]` (import at top: `from dbops.node_translation import STATUS_TO_STATE`); `migrations_nodes` uses `STATUS_TO_STATE[...]` in its backfill in place of `_THREAD_STATUS_MAP`. Verify no import cycle (`node_translation` imports nothing from these — it is leaf).
- [ ] **Step 4: Run** FULL `make test` → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(maps): delete 2 duplicate forward-maps; import canonical [H1]"`

**Step-2 DONE when:** `make test` green AND `grep -rn '"active": "open"' src/` → exactly 1 (the `db_mirror` 4th is semantically distinct and is eliminated by module deletion in Step 4 — see DA note D2).

---

# STEP 3 — Conversation cluster flip + delete the shim [C2 (conv), H2]

**Outcome:** the conversation read-source flips from `threads` to `nodes` AND its consumers adopt `state`/`title`/`last_active_at` IN THE SAME COMMITS (the rename is inseparable from the flip — see DA D1); the legacy conversation WRITES (`threads`) are cut; the unused alias-shim is deleted; conversation `except sqlite3.OperationalError: pass` divergence-hiders are removed.

**Expanded cluster (R2-1):** v1 omitted four conversation-cluster sites that read/write `background`. They are added here: the **watchdog reaper** (`juggle_watchdog.py:875` `SELECT * FROM threads WHERE status='background'`), the **background writer** `juggle_dispatch_core.py:103` (plus watchdog rebinds `:761,:955`), `juggle_cmd_context.py:145`, and `juggle_context_startup.py:32,200`. Because Task 2.0 made `background` a first-class state, each of these flips to `nodes` with the state PRESERVED (`state='background'`, NOT collapsed to `running`) — so the reaper still selects exactly the dispatched-agent conversations and the two cockpit panels (2a `state='running'`, 2b `state='background'`) stay disjoint. The Step-4 `.static.fail==0` gate WOULD otherwise force an unplanned ad-hoc flip of `watchdog:875`/`cockpit:276` with the naive map — that is the concrete break R2-1 prevents.

**Why a cluster, not a phase:** conversation status was historically written only to `threads`; a read flipped to `nodes` before the write flips reads stale state. The whole conversation cluster (write→nodes, read→nodes, consumer rename) is ONE atomic unit (DA H3). Dual-write already added the `nodes` mirror (`conv_node_mirror`), so the write side is staged — this step makes `nodes` the SOLE conversation writer and flips reads+consumers together.

**Step-3 monotonic gate:** `doctor --pre-p8-check --json` `.static.fail` strictly decreases (conversation-cluster `FROM threads` refs removed); `grep -rln "CONV_ALIAS_SHIM\|STATE_AS_STATUS_SQL" src/` → only files that legitimately reverse-map for a real reason (target: shim deleted).

---

### Task 3.0 — Delete the unused alias-shim (dead code, immediately green)

**Files:**
- Modify: `src/dbops/node_translation.py:28-37` (delete `STATE_AS_STATUS_SQL` shim usage + `CONV_ALIAS_SHIM`; keep `STATE_TO_STATUS`/`status_for_state` ONLY if a real consumer remains — see Task 3.1 audit)
- Test: `tests/test_node_translation.py`

- [ ] **Step 1: Write the failing test**
```python
def test_alias_shim_deleted():
    """2026-06-27 P8 H2: the speculative alias-shim must not exist — consumers
    adopt the new column names; no permanent re-aliasing layer."""
    from dbops import node_translation as nt
    assert not hasattr(nt, "CONV_ALIAS_SHIM")
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — confirm `grep -rln "CONV_ALIAS_SHIM" src/` shows ONLY `node_translation.py` (proven unused), then delete the constant. Keep `STATE_AS_STATUS_SQL` only if Step 3.1's audit finds a SQL site that genuinely needs reverse-mapping during the transition; otherwise delete it too.
- [ ] **Step 4: Run** FULL `make test` → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(shim): delete unused conversation alias-shim [H2]"`

---

### Task 3.1 — Flip conversation consumers to `nodes` + rename columns (atomic, per consumer)

**Files (consumers reading conversation rows):**
- `src/juggle_cockpit_model.py` — panel 2a `:270` (`status='running'` → `state='running'`), panel 2b `:276` (`status='background'` → `state='background'`), and the other `SELECT * FROM threads WHERE status='…'` panels (`:264-290`) → `FROM nodes WHERE kind='conversation' AND state='…'`, value-mapped; plus the tier helper `:123` `if status == "background"` → `if state == "background"`. **2a and 2b MUST stay disjoint** (they only do because `background` is now its own state — Task 2.0).
- `src/juggle_cmd_threads.py`, `src/juggle_cmd_projects.py`, `src/juggle_cmd_agents_lifecycle.py`, `src/juggle_cmd_runs.py`, `src/juggle_cmd_selfheal.py`, `src/juggle_project_summary.py` (`row['status']`/`['topic']`/`['last_active']` → `row['state']`/`['title']`/`['last_active_at']`, plus value compares `== 'active'` → `== 'open'`, `== 'background'` → `== 'background'`)
- **R2-1 ADDED conversation-cluster sites (v1 omitted these):**
  - `src/juggle_watchdog.py:875` — the agent-reaper `SELECT * FROM threads WHERE status='background'` → `FROM nodes WHERE kind='conversation' AND state='background'` (the `:879` join `agents WHERE status='busy'` is unchanged; the reaper still means "dispatched conversations with a busy agent"). Watchdog rebind WRITES `:761,:955` `update_thread(status="background")` flip to the node state-writer with `state='background'`.
  - `src/juggle_dispatch_core.py:103` — the background WRITER `db.update_thread(thread_id, status="background")` flips to writing `nodes.state='background'` (this is part of why the cluster is atomic: writer + reaper + panels co-commit).
  - `src/juggle_cmd_context.py:145` (`elif status == "background"`) and `src/juggle_context_startup.py:32,200` (`if status == "background"`) → branch on `state == "background"`.
  - `src/dbops/threads.py:431-465` `get_archive_candidates` — reads `status`; `status NOT IN ('background','waiting')` and `status == 'idle'` (`:460,455`). **`'waiting'`/`'idle'` are NOT in `STATUS_TO_STATE`** (no node-state equivalent). Step 1: `grep -rnE "update_thread\([^)]*(waiting|idle)|set_thread_status\([^)]*(waiting|idle)|status *= *['\"](waiting|idle)" src/` — if NO writer sets them (expected: they are display-only emoji states, never stored), DROP the dead `'waiting'`/`'idle'` arms when flipping to `state NOT IN ('background',)`; if a writer DOES set them, add the missing key to `STATUS_TO_STATE` first (fail-loud `state_for_status` would otherwise KeyError on such a row).
  - **`src/dbops/slug_alloc.py:26` `LIVE_SLUG_STATES = ("active","running","background")`** — the canonical live-thread set; flips to the node-state set `("open","running","background")`. `_live_labels`/`next_wheel_slug` (`:34,:117`) and `src/dbops/threads.py:89,96,242,367` (via `_OPEN_THREAD_STATES = LIVE_SLUG_STATES`) flip `threads`→`nodes WHERE kind='conversation'`.
  - **`src/dbops/migrations_recent.py:362` — the partial UNIQUE INDEX `idx_threads_live_label ON threads(user_label) WHERE status IN ('active','running','background')`.** A NEW conversation-cluster migration must reproduce it on `nodes(user_label) WHERE kind='conversation' AND state IN ('open','running','background')`. **⚠️ This is the slug-collision guard — the naive `background→running` collapse would drop `background` from the index predicate and recycle a LIVE background agent's slug onto a new thread (the exact pre-2026-06-21 incident the `next_wheel_slug` docstring names). This is the single most concrete proof that R2-1's first-class `background` is load-bearing, not cosmetic.** The existing pin `tests/test_thread_label_alloc_atomic.py` ("'background' is a LIVE state — its slug must not be recycled") is REWRITTEN to assert the same invariant over `nodes` (regression-pin gate: rewrite through the new seam, do not delete).
- Test: `tests/test_cockpit_model.py`, `tests/test_p8_conv_read_collapse.py`, `tests/test_cmd_threads.py`, `tests/watchdog/test_watchdog.py` (+ the R2-1 reaper pin below, + per-consumer tests)

**THIS IS NOT A PURE MECHANICAL RENAME (DA D3).** Each `row['status']` flip carries a VALUE translation (`'active'→'open'`, `'closed'→'done'`, **`'background'→'background'` — bijective, NOT collapsed to `running` per R2-1/Task 2.0**, `'running'→'running'`, `'archived'→'archived'`). Do each consumer as its own TDD task: write a test asserting the consumer's behavior over a `nodes`-seeded DB, flip the SELECT + the bracket access + the value compares together, run. **Use `node_translation.STATUS_TO_STATE` for every value compare — never re-type the map inline.**

- [ ] **Step 1: Write the failing test (per consumer)** — e.g. cockpit list:
```python
# tests/test_cockpit_model.py  (add)
def test_cockpit_lists_active_conversations_from_nodes(tmp_db):
    """2026-06-27 P8 C2: cockpit conversation panels read nodes (kind=conversation,
    state='open'), not threads.status='active'."""
    from juggle_add_node import add_node
    from juggle_cockpit_model import build_model   # adjust to real entrypoint
    add_node(tmp_db, kind="conversation", title="alpha", project_id="INBOX")
    model = build_model(tmp_db)
    assert any(r["title"] == "alpha" for r in model.active_conversations)
```

```python
# tests/watchdog/test_watchdog.py  (add) — the R2-1 reaper/panel pin
def test_reaper_distinguishes_background_from_live_conversation(tmp_db):
    """2026-06-27 P8 R2-1: the background-collapse bug. The watchdog reaper must
    select dispatched-agent (background) conversation nodes and EXCLUDE a live
    interactive conversation. Pre-fix (background->running collapse) this query
    would match both running and background, sweeping live conversations."""
    from juggle_add_node import add_node
    bg = add_node(tmp_db, kind="conversation", title="bg", project_id="INBOX")
    live = add_node(tmp_db, kind="conversation", title="live", project_id="INBOX")
    # dispatch flips bg's node to state='background'; live stays 'open'
    tmp_db.update_node_state(bg["node_id"], "background")        # via the node state-writer
    rows = tmp_db._connect().execute(
        "SELECT id FROM nodes WHERE kind='conversation' AND state='background'").fetchall()
    ids = {r[0] for r in rows}
    assert bg["node_id"] in ids and live["node_id"] not in ids

def test_cockpit_panels_2a_2b_disjoint(tmp_db):
    """2026-06-27 P8 R2-1: panels 2a (running) and 2b (background) must be disjoint —
    only true because 'background' is its own node state (Task 2.0)."""
    conn = tmp_db._connect()
    running = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE kind='conversation' AND state='running'")}
    background = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE kind='conversation' AND state='background'")}
    assert running.isdisjoint(background)
```
(`update_node_state`/the node state-writer name: use whatever Task 1.5 named the writer — `write_state(conn, node_id, 'background', now=…)`; adjust the helper call to the real seam.)
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — flip that consumer's SELECT to `nodes` with the `kind='conversation'` discriminator and the value-mapped state predicate (`node_translation.STATUS_TO_STATE`), and rename its row accesses. Repeat per consumer.
- [ ] **Step 4: Run** the consumer's tests, then FULL `make test`. For cockpit, also `uv run src/juggle_cli.py cockpit --smoke --all-viewports` and paste the summary.
- [ ] **Step 5: Commit** per consumer — `git commit -m "p8(conv-flip): <consumer> reads nodes, adopts state/title [C2,H2]"`

**Acceptance gate (agent):** after all conversation consumers flip —
- `grep -rnE "\['status'\]|\['topic'\]|\['last_active'\]" src/juggle_cmd_threads.py src/juggle_cmd_projects.py src/juggle_cmd_agents_lifecycle.py src/juggle_cmd_runs.py src/juggle_cmd_selfheal.py src/juggle_project_summary.py src/juggle_cockpit_model.py src/juggle_cmd_context.py src/juggle_context_startup.py` → **empty**.
- **R2-1 background:** `grep -rnE "status *== *['\"]background['\"]|status='background'|FROM threads WHERE status='background'" src/juggle_watchdog.py src/juggle_cockpit_model.py src/juggle_cmd_context.py src/juggle_context_startup.py src/juggle_cmd_threads.py src/juggle_dispatch_core.py` → **empty** (all flipped to `state='background'` on `nodes`).
- `test_reaper_distinguishes_background_from_live_conversation` + `test_cockpit_panels_2a_2b_disjoint` pass — the reaper set contains the busy-agent conversation and excludes the live one; panels 2a/2b are disjoint (reaper + cockpit counts unchanged pre/post flip, the R2-1 Agent-verify).

---

### Task 3.2 — Cut the legacy conversation WRITE; remove conversation divergence-hiders

**Files:**
- Modify: `src/dbops/threads.py` (make the `nodes` write the SOLE conversation write; the `threads` INSERT/UPDATE alongside `mirror_conv_*` is removed once reads no longer touch `threads`), `:108-109` remove `except sqlite3.OperationalError: pass`
- Modify: `src/dbops/conv_node_mirror.py:32` — narrow/remove the blanket `except OperationalError: pass` so a real schema gap fails LOUD (the H4 fix in Step 5 makes the DDL complete, so this no longer needs to be swallowed)
- Modify: `src/dbops/messages.py:77-78` remove the `except … pass` hider once `nodes` is guaranteed present
- **R2-1 background writers** — flip the three `update_thread(..., status="background")` writers to also write `nodes.state='background'` via the node state-writer (Task 1.5 `write_state`): `src/juggle_dispatch_core.py:103`, `src/juggle_watchdog.py:761,955`. (Pre-flip these already mirror through `conv_node_mirror`, which — after Task 2.0 — now maps `background→'background'`; this step makes the node write authoritative and drops the `threads` write.)
- Test: `tests/test_p8_conv_read_collapse.py`, `tests/test_add_node.py`

> **Ordering caveat:** narrowing `conv_node_mirror`'s `except` (fail-loud) DEPENDS on Step 5's complete DDL (else a fresh-DDL DB raises). If Step 3 must land first, narrow the `except` to catch ONLY "no such table: nodes" (pre-Migration-44) and re-raise a missing-COLUMN error; the full removal completes in Step 5 Task 5.1.

- [ ] **Step 1: Write the failing test** — assert a conversation create writes exactly one authoritative row and a missing `nodes` COLUMN raises (not silently swallowed).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the write-cut + except-narrowing.
- [ ] **Step 4: Run** FULL `make test` → green; `doctor --pre-p8-check --json` `.static.fail` strictly lower than Step-2 baseline.
- [ ] **Step 5: Commit** — `git commit -m "p8(conv-flip): nodes is sole conversation writer; remove silent except [C2,H4]"`

**Step-3 DONE when:** `make test` green AND the Task-3.1 greps (incl. the R2-1 `background` grep) are empty AND the R2-1 reaper/panel pins pass AND `doctor --pre-p8-check --json` `.static.fail` strictly < Step-2 value AND `CONV_ALIAS_SHIM` deleted AND `python -c "import dbops.threads, juggle_watchdog, juggle_dispatch_core, juggle_cmd_context, juggle_context_startup"` exits 0.

---

# STEP 4 — Graph cluster flip; delete legacy writes + reads; complete C1 [C2 (graph), C1]

**Outcome:** the graph-topic/task read-source flips from `graph_topics`/`graph_tasks` to `nodes`; `db_mirror.py` is DELETED (mirror concept dead); `add_node` stops the `graph_tasks`/`graph_edges` INSERTs; the engine's state-writer drops its `graph_*` half (so `graph_tasks` is NEVER written for the task lifecycle — completing C1); `orphan_guard`/`db_topics_reconcile` compat-reads of `graph_topics` are removed; every remaining `except sqlite3.OperationalError: pass` that hid graph divergence is removed.

**Why this is the second cluster:** the task-execution readers (`ready_eligible`, `claim_task`, `get_task`, `unverified_deps`) and the cockpit DAG/orphan_guard reads all key off `graph_*`. They flip to `nodes` together with the write-cut, atomically, so no reader sees a stale or missing row.

**Step-4 monotonic gate:** `doctor --pre-p8-check --json` `.static.fail` → **0** (all live steady-state legacy refs gone); `grep -rnE "INSERT INTO graph_tasks|INSERT INTO graph_edges|INSERT OR IGNORE INTO graph_" src/juggle_add_node.py` → empty; `grep -rnE "FROM threads|FROM graph_topics|FROM graph_tasks" src/juggle_cockpit_model.py src/dbops/orphan_guard.py` → empty.

## Step-4 rollback (R2-3) — Step 4 is the irreversibility boundary, NOT Step 6

**Why this section exists:** the spec's §12.2 rollback ("old tables were never dropped in P1–P7; `DROP TABLE nodes`; system resumes") is valid ONLY while **legacy stays authoritative** — true through Step 3, **false from Task 4.3 onward**. Once 4.3 cuts the `graph_tasks`/`graph_edges` writes, new task/edge rows live ONLY in `nodes`/`node_edges`. A bare `git revert` of the Step-4 commits restores legacy-*reading* code, but nothing back-fills `graph_*` from the nodes-only rows created during the Step-4+ window → those tasks silently vanish from the executor. So Step 4 needs its OWN rollback, not just Step 6's.

**Chosen recovery (do BOTH — backup is the floor, reverse-backfill is the clean path):**

1. **Pre-flip backup (mandatory, before the Task 4.3 commit reaches production).** Mirror the doctor backup idiom: one-shot copy the live DB to `~/.claude/juggle/juggle.db.bak-pre-p8-step4` BEFORE 4.3. This is the no-data-loss floor: "Step 4+ rollback = stop juggle, restore from `juggle.db.bak-pre-p8-step4`, deploy the pre-Step-4 binary." Recovery loses only post-backup work (acceptable for a revert).
2. **Documented reverse-backfill (the clean path, no full restore).** Author `dbops/p8_reverse_backfill.py::reverse_backfill_nodes_to_graph(conn)` — idempotent, the inverse of `migration_nodes_parity`/`conv_node_mirror`: for every `kind='task'` node it re-INSERTs (OR IGNORE) the equivalent `graph_tasks` row (state value-mapped back via `node_translation.status_for_state`) and re-creates `graph_edges` from `node_edges WHERE kind='dep'`. Running it after a `git revert` of the Step-4 reads makes the nodes-only rows visible to the restored legacy engine WITHOUT a full DB restore. This is the documented Step-4 rollback procedure; ship the function in the SAME PR as Task 4.3 (it is dead in the forward path, live only on revert).

- [ ] **Rollback pin (RED test, lands with Task 4.3):**
```python
# tests/test_p8_reverse_backfill.py
def test_reverse_backfill_reconstructs_graph_tasks(tmp_db):
    """2026-06-27 P8 R2-3: after the Step-4 write-cut, a nodes-only task must be
    reconstructable into graph_tasks so a revert of legacy-reading code can see it."""
    from juggle_add_node import add_node
    from dbops.p8_reverse_backfill import reverse_backfill_nodes_to_graph
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")   # nodes-only post-4.3
    conn = tmp_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM graph_tasks WHERE id=?", (r["node_id"],)).fetchone()[0] == 0
    reverse_backfill_nodes_to_graph(conn); conn.commit()
    row = conn.execute("SELECT state FROM graph_tasks WHERE id=?", (r["node_id"],)).fetchone()
    assert row is not None   # legacy engine can now see the task
    reverse_backfill_nodes_to_graph(conn)   # idempotent — second run no-ops
```

**Agent-verify (R2-3):** the plan names a Step-4 rollback procedure (above); `test_reverse_backfill_reconstructs_graph_tasks` passes; the Task-4.3 runbook step "take `juggle.db.bak-pre-p8-step4` before commit" is present (Task 4.3 Step 0 below).

---

### Task 4.1 — Flip task-execution reads to `nodes`

**Files:**
- Modify: `src/dbops/db_graph.py` (`get_task`, `get_task_by_thread`, `list_tasks`, `ready_eligible`, `unverified_deps`, `get_deps`/`get_dependents`/`replace_edges` → `nodes`/`node_edges` with `kind='task'`), `src/juggle_graph_dispatch.py` (`claim_task`/`sweep_stale_claims` CAS → `nodes`)
- Test: `tests/test_db_graph.py`, `tests/test_graph_dispatch.py`, `tests/test_graph_scheduler.py`

- [ ] **Step 1: Write the failing test** — assert `ready_eligible`/`claim_task` operate when ONLY `nodes`/`node_edges` carry the task (seed no `graph_tasks` row).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the read repoint (task = `kind='task'`; edges from `node_edges`).
- [ ] **Step 4: Run** FULL `make test` → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(graph-flip): task-execution reads from nodes/node_edges [C2]"`

---

### Task 4.2 — Flip topic-tier + DAG + orphan_guard reads; delete `db_mirror`

**Files:**
- Modify: `src/dbops/db_topics.py` (CRUD/queries → `nodes` `kind='task' AND parent_id IS NULL`), `src/dbops/db_topics_reconcile.py`, `src/juggle_cockpit_graph_dag.py` (drop the legacy fallback; read `nodes` only), `src/dbops/orphan_guard.py:48-66,121-132,164-184,214-224` (delete the `graph_topics` compat lookups; resolve thread/dispatch binding via the Step-6 dispatch-edge or `nodes`)
- Delete: `src/dbops/db_mirror.py` + **ALL** its importers — enumerate with `git grep -l db_mirror -- src/` (do NOT hand-list). At commit `cyc_EC@4a06d8e` the importers are: `dbops/threads.py`, `dbops/db_topics.py`, `dbops/db_topics_reconcile.py`, `juggle_cmd_doctor.py`, `juggle_cmd_projects.py`, **`juggle_cmd_threads.py`** — i.e. **`threads.py` + `cmd_threads.py` that v1 missed (R2-5)**, plus the mirror-backfill block in `juggle_cmd_doctor.py`. Removing the module without flipping `dbops/threads.py` and `juggle_cmd_threads.py` raises `ImportError` at this commit (green-per-commit break).
- Test: `tests/test_cockpit_graph_dag_load.py`, `tests/test_graph_mirror.py` (delete or rewrite — mirror concept gone), `tests/test_db_mirror.py` (delete), `tests/test_graph_reconcile.py`

> Per the regression-pin gate: `test_db_mirror.py`/`test_graph_mirror.py` assert behavior of a deleted concept. Deleting them is allowed (obsolete tests), but FIRST confirm no pin inside them guards a still-live invariant (e.g. "mirror topics never dispatched"); if so, rewrite that pin to assert the equivalent over `nodes` (conversation nodes never enter the task dispatch set).

- [ ] **Step 1: Write the failing test** — assert the cockpit DAG renders topic+task nodes purely from `nodes` with no `graph_*` table present.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the repoint + `db_mirror` deletion + caller removal.
- [ ] **Step 4: Run** FULL `make test`; `cockpit --smoke --all-viewports`.
- [ ] **Step 5: Commit** — `git commit -m "p8(graph-flip): topic/DAG/orphan reads from nodes; delete db_mirror [C2,H1]"`

**Acceptance gate (agent, R2-5):** `grep -rl db_mirror src/` → **empty**; `python -c "import dbops.threads, dbops.db_topics, dbops.db_topics_reconcile, juggle_cmd_threads, juggle_cmd_projects, juggle_cmd_doctor"` exits 0 (every former importer flipped); `test -f src/dbops/db_mirror.py` false.

---

### Task 4.3 — Cut the legacy WRITES (`add_node` + engine) — completes C1

**Files:**
- Modify: `src/juggle_add_node.py:216-246` (delete the `graph_tasks` INSERT + `graph_edges` INSERTs; keep `nodes` + `node_edges`); update the module docstring (drop the dual-write contract)
- Modify: `src/dbops/state_write.py` (drop the `graph_tasks`/`graph_topics` mirror half — `nodes` only)
- Modify: any remaining `except sqlite3.OperationalError: pass` hiding a graph write (`conv_node_mirror`, `messages`, `threads`) — remove now that `nodes` is guaranteed present
- Create: `src/dbops/p8_reverse_backfill.py` (R2-3 rollback inverse — `reverse_backfill_nodes_to_graph(conn)`; idempotent; nodes→graph_tasks/graph_edges with `status_for_state` value-map)
- Test: `tests/test_add_node.py`, `tests/test_dispatch_node.py`, `tests/test_graph_autopilot_integration.py`
- Test: `tests/test_p8_reverse_backfill.py` (the R2-3 rollback pin from the Step-4 rollback section)

- [ ] **Step 0: Take the irreversibility backup (R2-3).** This commit makes `nodes` the SOLE store. BEFORE it lands in production, snapshot the live DB: `cp ~/.claude/juggle/juggle.db ~/.claude/juggle/juggle.db.bak-pre-p8-step4` (orchestrator/runbook step — the agent records this requirement; the actual copy runs at deploy). Rollback if reverted = restore this backup OR run `reverse_backfill_nodes_to_graph` (Step-4 rollback section). Also create `src/dbops/p8_reverse_backfill.py` in THIS commit (dead in the forward path; live only on revert).
- [ ] **Step 1: Write the failing test** — the C1 capstone pin:
```python
def test_task_lifecycle_never_writes_graph_tasks(tmp_db):
    """2026-06-27 P8 C1: a task node driven open->ready->...->done via the unified
    engine must NEVER write graph_tasks (single store = nodes)."""
    from dbops import db_graph
    from juggle_add_node import add_node
    r = add_node(tmp_db, kind="task", title="x", project_id="INBOX")
    # graph_tasks may still EXIST (dropped in Step 6) but must hold zero rows for this id:
    n = tmp_db._connect().execute(
        "SELECT COUNT(*) FROM graph_tasks WHERE id=?", (r["node_id"],)).fetchone()[0]
    assert n == 0
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the write-cut.
- [ ] **Step 4: Run** FULL `make test` → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(graph-flip): cut legacy graph_* writes; nodes is sole store [C1,C2]"`

**Step-4 DONE when:** `make test` green AND `doctor --pre-p8-check --json` `.static.fail` == **0** AND the three Step-4 greps are empty AND the C1 capstone pin passes AND a test drives a task `open→ready→dispatching→running→integrating→verified→done` through `node_transition` only, asserting `graph_tasks` unwritten (C1 Agent-verify, full). AND (R2-5) `grep -rl db_mirror src/` empty AND (R2-3) `test_reverse_backfill_reconstructs_graph_tasks` passes + the `juggle.db.bak-pre-p8-step4` runbook step is recorded.

---

# STEP 5 — Honest DDL + honest Gate-A (BEFORE any irreversible drop) [H4, M4]

**Outcome:** `CREATE_NODES` is complete (a fresh DB built from the DDL alone has every column the code writes); the Gate-A scanner no longer has blind spots (it scans the former-legacy-engine modules and reports import-reachability + the files it excluded). These MUST be true before the Step-6 drop.

**Step-5 monotonic gate:** `PRAGMA table_info(nodes)` after `CREATE_NODES`-only ⊇ the mirror's column set; `doctor --pre-p8-check --json` includes `excluded_files` (a list) and `import_refs` (== 0 for the legacy engines).

---

### Task 5.1 — Fold the 4 parity columns into `CREATE_NODES` (DDL honesty) [H4]

**Files:**
- Modify: `src/dbops/schema_nodes.py:8-57` (add `user_label TEXT`, `assigned_by TEXT NOT NULL DEFAULT 'auto'`, `last_active_at TEXT`, `dispatch_thread_id TEXT` — keep the additive ALTERs in `migration_nodes_parity` as idempotent no-ops for upgrades)
- Modify: `src/dbops/conv_node_mirror.py:32` — now safely REMOVE the blanket `except OperationalError: pass` (fresh DDL is complete, so a real gap fails loud)
- Test: `tests/test_nodes_schema_migration.py`

- [ ] **Step 1: Write the failing test**
```python
def test_create_nodes_is_complete():
    """2026-06-27 P8 H4: a fresh nodes table from CREATE_NODES alone (no migrations)
    must contain every column conv_node_mirror writes."""
    import sqlite3
    from dbops.schema_nodes import CREATE_NODES
    conn = sqlite3.connect(":memory:"); conn.execute(CREATE_NODES)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    for c in ("user_label","assigned_by","last_active_at","dispatch_thread_id",
              "session_id","summarized_msg_count","show_in_list"):
        assert c in cols, f"CREATE_NODES missing {c}"
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — add the 4 columns to `CREATE_NODES`; remove the swallow in `conv_node_mirror`.
- [ ] **Step 4: Run** FULL `make test` (incl. `doctor --dry-run` smoke on a fresh tmp DB) → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(ddl): fold 4 parity columns into CREATE_NODES; fail-loud mirror [H4]"`

---

### Task 5.2 — Honest Gate A: scan the de-excluded engines + report exclusions/imports [M4]

**Files:**
- Modify: `src/dbops/p8_readiness.py` (`_excluded` no longer skips `db_graph`/`db_topics`/`db_mirror` — they are no longer schema/migration files; add `import_refs(src_root)` that asserts no module imports a still-legacy engine and that `db_mirror` is gone; `pre_p8_report` emits `excluded_files` + `import_refs`)
- Modify: `src/juggle_cmd_doctor_p8.py` (surface the new fields)
- Test: `tests/test_p8_readiness.py`

- [ ] **Step 1: Write the failing test**
```python
def test_gate_a_reports_exclusions_and_imports(tmp_path):
    """2026-06-27 P8 M4: the gate must log which files it skipped and assert the
    legacy engines are unreachable — no PASS:0 while db_mirror still imports."""
    from pathlib import Path
    from dbops.p8_readiness import pre_p8_report
    import sqlite3
    rep = pre_p8_report(sqlite3.connect(":memory:"), Path("src"))
    assert isinstance(rep["static"]["excluded_files"], list)
    assert rep["static"]["import_refs"] == 0   # db_mirror deleted; no legacy-engine imports
```
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — de-exclude the engine modules; add the import-reachability check (grep `import db_mirror`/`from dbops.db_mirror`; assert `db_mirror.py` absent); populate `excluded_files`.
- [ ] **Step 4: Run** FULL `make test` → green.
- [ ] **Step 5: Commit** — `git commit -m "p8(gate): honest Gate-A — scan de-excluded engines, log exclusions+imports [M4]"`

**Step-5 DONE when:** `make test` green AND `test_create_nodes_is_complete` + `test_gate_a_reports_exclusions_and_imports` pass AND `doctor --pre-p8-check --json` reports `static.fail==0`, `import_refs==0`, and a populated `excluded_files`.

---

# STEP 6 — Post-collapse cleanup + terminal drop + spec rewrite [M1, M2, M3, L1, H5]

**Outcome:** the dispatch relation is modelled explicitly (M1); the kind discriminator is enforced (M2); the M3 atomicity pin is in place (added in Step 1, re-asserted here); `juggle_migrate_lifecycle.py` is deleted (L1); the legacy tables are physically dropped behind the now-honest gate; the spec is demoted to as-built (H5). After this step there is exactly ONE model and ONE machine.

---

### Task 6.1 — Model the dispatch relation explicitly [M1, Q2]

**Files (recommended: typed `node_edges` edge — see OQ2):**
- Create: `src/dbops/migration_52_dispatch_edge.py` (add `node_edges.kind TEXT NOT NULL DEFAULT 'dep'`; migrate `nodes.dispatch_thread_id` → a `kind='dispatch'` edge `(task_node_id, conversation_node_id)`; then drop `dispatch_thread_id` in the terminal rebuild)
- Modify: every `node_edges` query to filter `kind='dep'` for dependency logic; `kind='dispatch'` for the agent-thread binding (`orphan_guard`, dispatch hydration)
- Test: `tests/test_migration_52_dispatch_edge.py`, `tests/test_dispatch_node.py`

- [ ] **Step 1:** RED test — the task→dispatch-thread link round-trips through the typed edge.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement migration 52 + query updates. Document the edge kinds in `schema_nodes.py`.
- [ ] **Step 4:** FULL `make test` → green.
- [ ] **Step 5:** Commit — `git commit -m "p8(cleanup): model dispatch relation as typed node_edge [M1]"`

**Acceptance gate (agent):** `grep -rn "dispatch_thread_id" src/` → only the migration that retires it; dispatch round-trip test passes.

---

### Task 6.2 — Enforce the kind discriminator [M2]

**Files:**
- Modify: `src/dbops/schema_nodes.py` (add `CHECK` constraints, e.g. `CHECK (kind='task' OR verify_cmd IS NULL)`, and conversation-only columns NULL for non-conversation) — added on the rebuild in the terminal drop migration (SQLite can't add CHECK via ALTER), OR a single insert/update guard in the node-write path if the rebuild is deferred
- Test: `tests/test_nodes_schema_migration.py`

- [ ] **Step 1:** RED test — each illegal cross-kind insert is rejected; `SELECT COUNT(*) FROM nodes WHERE kind='conversation' AND verify_cmd IS NOT NULL` pinned at 0.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the CHECK/guard. Document the wide-table-with-discriminator decision (do NOT split the table).
- [ ] **Step 4:** FULL `make test` → green.
- [ ] **Step 5:** Commit — `git commit -m "p8(cleanup): enforce kind discriminator on nodes [M2]"`

---

### Task 6.3 — Terminal drop migration + delete `juggle_migrate_lifecycle.py` [L1] (capstone — see OQ1)

**Files:**
- Create: `src/dbops/migration_53_p8_drop.py` (BEGIN IMMEDIATE fail-loud; gated at top by `p8_readiness.p8_drop_ready`; FK-repoint `messages`/`notifications`/`notifications_v2`/`action_items` `thread_id REFERENCES nodes(id)` via the table-rebuild idiom; DROP `graph_edges`,`graph_tasks`,`graph_topics`,`threads` in FK order; rebuild `nodes` with the Step-6.2 CHECK constraints + drop `dispatch_thread_id`)
- Modify: `src/juggle_db.py:141,155-157` (DELETE the base `CREATE_THREADS`/`CREATE_GRAPH_*` lines so `init_db` does not re-create the dropped tables — the sharp edge from the doctor research §4.4)
- Modify: `src/dbops/schema_graph.py` (remove dead `CREATE_GRAPH_*` constants), `src/dbops/schema.py` (drop `CREATE_THREADS` + the 4 FK clauses)
- Delete: `src/juggle_migrate_lifecycle.py`
- Modify: `src/dbops/migrations_recent.py` (wire migration 53 after 52)
- Test: `tests/test_doctor.py`, `tests/test_p8_readiness.py`, a new `tests/test_migration_53_p8_drop.py`

- [ ] **Step 1:** RED tests — (a) `p8_drop_ready`-green tmp DB → migration drops all 4 tables, `init_db` does NOT re-create them, messages repointed; (b) `p8_drop_ready`-blocked DB → no drop, no backup, reasons surfaced; (c) `test -f src/juggle_migrate_lifecycle.py` false.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement the drop (backup-before-drop to `~/.claude/juggle/juggle.db.bak-pre-p8` per the doctor research §4.1); delete `juggle_migrate_lifecycle.py` and its 9 refs.
- [ ] **Step 4:** FULL `make test`; `doctor --dry-run` then real `doctor` against a tmp DB → converges to `already-dropped`.
- [ ] **Step 5:** Commit — `git commit -m "p8(drop): terminal legacy-table drop behind honest gate; delete migrate_lifecycle [L1]"`

**Acceptance gate (agent):** after a real `doctor` on a ready tmp DB, `p8_drop_ready(conn)` → `(False, ['already-dropped'])`; `sqlite_master` has no `threads`/`graph_*`; `make test` green; `test -f src/juggle_migrate_lifecycle.py` false.

---

### Task 6.4 — Rewrite the spec as as-built [H5]

**Files:**
- Modify: `specs/2026-06-18-unified-topic-graph.md` (header `LOCKED design` → `SUPERSEDED — see as-built addendum`; add an addendum recording the resolved Q1–Q4, the single-model/single-machine end-state, the typed dispatch edge, and that §10's "eliminated" list is now actually true)
- Test: a doc/reviewer check — `grep -n "LOCKED" specs/2026-06-18-unified-topic-graph.md` returns no bare `LOCKED` without an "as-built status" line; assert each §10 "eliminated" item is absent from `src/`.

- [ ] **Step 1:** RED — a tiny `tests/test_spec_as_built.py` asserting `dispatch_thread_id` (raw column) and `'pending'` are absent from `src/` and the spec no longer claims `LOCKED` without an as-built note.
- [ ] **Step 2:** Run → FAIL (until the spec is edited).
- [ ] **Step 3:** Edit the spec.
- [ ] **Step 4:** Run → PASS; FULL `make test`.
- [ ] **Step 5:** Commit — `git commit -m "p8(docs): demote spec to as-built; record resolved Q1-Q4 [H5]"`

**Step-6 DONE when:** `make test` green AND legacy tables physically gone (`p8_drop_ready` → already-dropped) AND `dispatch_thread_id`/`'pending'`/`db_mirror` absent from `src/` AND `juggle_migrate_lifecycle.py` deleted AND the spec test passes.

---

## Plan-DA round 2 (R2) coverage

| Finding | Sev | Covered by | Acceptance gate |
|---|---|---|---|
| R2-1 (`background` collapse) | CRIT | Task 2.0 (first-class bijective state + machine transitions); Step 3.1/3.2 (watchdog reaper, dispatch_core/watchdog writers, cmd_context, context_startup, cockpit 2a/2b) | bijection round-trip; `node_transition('background',…,'conversation')` no-raise; reaper-vs-live pin; panels-disjoint pin; `background` grep empty |
| R2-2 (dangling `_TRANSITIONS` import) | HIGH | Tasks 1.3+1.4 merged into ONE commit; `_cx` kept | `pytest --co` zero import errors at the shared commit; `import dbops.db_topics` exits 0 |
| R2-3 (Step-4 rollback) | HIGH | Step-4 rollback section + Task 4.3 Step 0 backup + `p8_reverse_backfill.py` | reverse-backfill pin; `juggle.db.bak-pre-p8-step4` runbook step recorded |
| R2-4 (M51 fail-soft) | MED | Task 1.1 — M51 now `BEGIN IMMEDIATE` fail-LOUD | `test_migration_51_fail_loud_on_lock` |
| R2-5 (`db_mirror` callers) | MED | Task 4.2 — full `git grep -l db_mirror` enumeration incl. `threads.py`/`cmd_threads.py` | `grep -rl db_mirror src/` empty; former-importer import smoke |
| R2-6 (M44/M51 double-migration) | LOW | Accepted; noted at Task 1.1 | n/a (documented) |

---

## Self-Review (against the 13 DA findings)

| Finding | Covered by | Acceptance gate |
|---|---|---|
| C1 (dead machine / legacy engine) | 1.3, 1.4, 1.5, 4.3 | `node_transition` call sites >0; C1 capstone pin (graph_tasks unwritten) |
| C2 (dual-write/dual-read) | 3.1, 3.2, 4.1–4.3 | `.static.fail`→0; add-node single-row pin |
| C3 (two vocabularies) | 1.1–1.4 | `'pending'` grep empty; `SELECT DISTINCT state,kind FROM nodes` no pending |
| H1 (triplicated map) | 2.1, 2.2, (4.2 db_mirror) | `'"active": "open"'` grep → 1; SQL/dict parity pin |
| H2 (alias-shim) | 3.0, 3.1 | `CONV_ALIAS_SHIM` deleted; bracket-consumer grep empty |
| H3 (no green flip) | whole plan (cluster framing) | `.static.fail` strictly ↓ per cluster commit; full suite per commit |
| H4 (DDL lies) | 5.1 | `test_create_nodes_is_complete` |
| H5 (stale spec) | 6.4 | `test_spec_as_built` |
| M1 (dispatch_thread_id) | 6.1 | dispatch round-trip pin; raw column absent |
| M2 (discriminator) | 6.2 | illegal cross-kind insert rejected |
| M3 (atomicity) | 1.5 (pin), re-asserted 6.x | M3 no-drift pin |
| M4 (gate blind spots) | 5.2 | `import_refs==0`; `excluded_files` populated |
| L1 (dead migrate_lifecycle) | 6.3 | file absent; static floor drops |
| R2-1 (background first-class) | 2.0, 3.1, 3.2 | bijection round-trip + reaper/panel pins (see R2 table) |

**Placeholder scan:** none — every code step shows the code or the exact edit location + the RED test.
**Type consistency:** `node_transition(state,event,kind)`, `legal_events(kind)`, `write_state(conn,node_id,new_state,*,now,extra)`, `cas_state(conn,node_id,*,frm,to,now)`, `p8_drop_ready(conn)->(bool,list)`, `pre_p8_report(conn,src_root)->dict`, `reverse_backfill_nodes_to_graph(conn)`, `migrate_51_state_vocab(conn)` are used consistently across tasks. The `background` state value is the SAME string in `STATUS_TO_STATE`, `STATE_TO_STATUS`, `_NODE_TRANSITIONS`, and every flipped consumer (no `running` alias).

---

## Devil's Advocate — sequencing & assumption audit (done before finalizing)

**D1 — Is the ~107-consumer rename independently shippable BEFORE the flip? NO.** The shim's entire purpose was to let consumers keep reading `row['status']` after reads flip to `nodes` (where the column is `state`). Renaming `row['status']→row['state']` while the SELECT still hits `threads` (which has `status`, not `state`) is an immediate `KeyError`. **Mitigation (folded in):** the rename is NOT a standalone step — it is co-committed with each consumer's read-source flip (Step 3 for conversation consumers, Step 4 for graph consumers). The task's "step 3 = rename + drop shim" is realized as: delete the *dead* shim up front (3.0, trivially green) + flip-and-rename each consumer atomically (3.1). The greppable `['status']` gate is the JOINT acceptance of Steps 3–4, not a pre-flip step. This is the single biggest deviation from the literal DA ordering and is the only way to keep the suite green.

**D2 — Is the rename "safely mechanical"? NO — it hides VALUE reads.** Every `row['status']` flip carries a value translation (`'active'→'open'`, `'closed'→'done'`, `'background'→'background'` and `'running'→'running'` — **distinct, NOT collapsed**, per R2-1/Task 2.0, `'archived'→'archived'`). A blind rename that forgets the value-map silently changes behavior (e.g. a cockpit panel filtering `state=='active'` matches nothing; or — the R2-1 bug — folding `background` into `running` so the reaper sweeps live conversations). **Mitigation:** each consumer is its own TDD task with a behavior test over a `nodes`-seeded DB (3.1), and the value map comes from the single `node_translation.STATUS_TO_STATE` (Step 2, made bijective in Task 2.0) — never re-typed inline.

**D3 — Does delegating `db_graph→node_machine` break `pending` data already in DBs? YES, without a migration.** Existing prod/test DBs store `graph_tasks.state='pending'` and `nodes.state='pending'` (the latter from `backfill_graph_parity`). The renamed engine queries `state='open'` and `node_transition` has no `('pending',…)` entry → `ready_eligible` returns nothing and any transition on a migrated row raises. **Mitigation:** Migration 51 (Task 1.1) lands FIRST and is wired BEFORE the engine rename ships in the same release, rewriting `pending→open` across `graph_tasks`/`graph_topics`/`nodes`; idempotent; covered by RED tests. This is also why Task 1.2 deletes `backfill_graph_parity`'s `open→pending` re-introduction in the SAME step.

**D4 — Is the conversation cluster truly atomic?** Conversation status was historically written only to `threads`; dual-write later added the `nodes` mirror (`conv_node_mirror`). So the WRITE side is already staged in `nodes` before Step 3. The risk is the reverse: if a read flips to `nodes` but a writer still updates only `threads`, the node goes stale. **Mitigation:** Step 3 makes `nodes` the SOLE conversation writer (3.2) in the same step as the read flip (3.1); `conv_node_mirror`'s silent `except` is narrowed (3.2) and fully removed once the DDL is complete (5.1). The 5 cockpit panels + 7 consumer files flip together (3.1) so no panel reads `threads` while another reads `nodes`.

**D5 — "Monotonically reducing surface" is not a single counter.** Steps 1–2 do NOT reduce the legacy-table-ref count (`.static.fail`) — they keep dual-write ON; they reduce DIFFERENT counters (`'pending'` count → 0; duplicate-map count → 1). Claiming `.static.fail` strictly decreases at *every* step would be false. **Mitigation:** the Step→Counter table makes each step's monotonic metric explicit; `.static.fail` strictly decreases across the CLUSTER steps (3, 4) and the terminal drop (6), per the DA H3 Agent-verify, while Steps 1–2 drive their own divergence counters to floor. The composite legacy surface is non-increasing throughout.

**D6 — `db_mirror`'s map is the divergent 4th, but is it the SAME map?** No — `_THREAD_TO_STATE = {active:running, idle:pending, done:verified}` maps thread status → mirror-TOPIC state (graph_topics vocab), not → node state. Forcing it onto `node_translation.STATUS_TO_STATE` (which yields node states) would be wrong. **Mitigation:** Step 2 centralizes the THREE genuinely-identical thread→node maps; the divergence is ELIMINATED in Step 4 by deleting `db_mirror.py` wholesale (the mirror concept is dead once conversations are first-class nodes). This is cleaner than re-pointing a module that is about to be deleted.

**D7 — `db_topics` imports `db_graph._TRANSITIONS` directly (`:15`).** Deleting `_TRANSITIONS` in Task 1.3 would break `db_topics` import-time. **Mitigation:** Task 1.4 is sequenced in the SAME step and updates `db_topics.topic_transition` to delegate to `node_transition`; the two tasks land together (Step 1) so no commit leaves a dangling import. (If executed as separate commits, 1.3 must keep a transitional `_TRANSITIONS` re-export until 1.4 lands — call this out to the coder.)

**D8 — The terminal drop is irreversible; could a stale checkout drop tables the running code still needs?** **Mitigation:** the drop is gated by BOTH the honest Gate A (`import_refs==0`, `.static.fail==0`, Step 5) AND the runtime `p8_drop_ready` anti-join, runs only in the orchestrator context behind `assert_migration_allowed` (G2), takes a one-shot backup first, and is idempotent (re-run → `already-dropped`). Recommend (OQ1) shipping the drop in its own final PR after the gate is green in production for ≥1 release.

**D9 (R2-1) — Does adding `background` as a state keep every EXISTING test green?** Two suite pins encode the OLD `background→running` (`test_node_translation.py:8,23`, `test_node_transition.py:164`); they are REWRITTEN in Task 2.0 under the R2-1 pin-rewrite authorization (same seam, corrected value), not silently broken. A third, `test_data_migration.py:50 test_background_maps_to_running`, pins the SEPARATE dying `juggle_migrate_lifecycle` (threads-level) and is left untouched until that module is deleted in Task 6.3. No `conv_node_mirror` test asserts the old `'running'` mirror value (grep-verified empty), so the map flip is otherwise transparent. The slug-allocation subsystem (`slug_alloc.LIVE_SLUG_STATES`, the `idx_threads_live_label` partial unique index) and `test_thread_label_alloc_atomic.py` ALREADY treat `background` as a live state distinct from `running` — independent corroboration that the v1 collapse was wrong; those pins are rewritten over `nodes` in Step 3.1, not broken. **Residual:** if a future test seeds a thread `status='running'` and expects it to mean "background", it would now be wrong — but that conflation IS the bug; the bijection is the fix.

**D10 (R2-1) — Does any TASK-state query now accidentally match `'background'`?** No. `background` is written ONLY for `kind='conversation'` nodes (the writers are dispatch_core + watchdog rebinds, all conversation dispatches). Every task-execution query filters `kind='task'` (ready_eligible/claim_task/get_task) and keys on `open/ready/dispatching/running/integrating/verified/done` — none is `background`. The reaper (`watchdog:875`) explicitly filters `kind='conversation' AND state='background'`. So no task query can see a `background` row, and no conversation `background` row enters the dispatch set (re-asserted by the M2 discriminator + the "conversation nodes never dispatched" invariant carried over from the deleted mirror tests in Task 4.2).

**D11 (R2-1) — Is the mapping truly invertible EVERYWHERE it is used?** The forward `STATUS_TO_STATE` still has legacy-only many-to-one entries (`closed→done` AND `done→done`; `failed→failed-exec`) — these are NON-LIVE (`set_thread_status._VALID_STATES={active,running,closed,archived}` cannot emit `done`/`failed`; they appear only in historical rows). Invertibility is REQUIRED and PROVEN only over the LIVE vocab `{active,background,running,closed,archived}` (the `test_status_state_bijective_over_live_vocab` pin). The reverse `status_for_state` is the read-path direction (nodes→legacy status for any code still emitting status during the transition) and is exact for every live state including `background`. `'waiting'`/`'idle'` have NO node-state and NO writer (Task 3.1 proves them dead before dropping their archive-candidate arms) — so `state_for_status` is never called on them. **Residual:** the legacy-only `done`/`failed` inputs remain non-invertible, but they are out of the live set and disappear with the `threads` table drop in Step 6.

---

## Open Questions (batched — do NOT block planning)

> **Resolved by R2-1 (NOT open — baked decision, do not re-litigate):** `background` is kept as a distinct first-class node state (option (a) of the DA's R2-1 fix direction). The alternative — DERIVING background-ness from the agent binding (`agents.status='busy' AND assigned_node`) and splitting "focus" (foreground/background) from "lifecycle" into a separate dimension (option (b)) — is a FUTURE refinement, explicitly OUT of P8 scope. P8's bar is ONE model + ONE machine with the existing distinctions preserved losslessly; the dimension split is a clean-architecture follow-up, not a completion blocker.


1. **OQ1 — Terminal drop timing.** Author the drop migration in this plan (Task 6.3) but ship it in a SEPARATE final PR after `doctor --pre-p8-check` has reported green in production for ≥1 release (recommended, safest), OR ship it bundled with Step 5? Either keeps the gate; the question is soak time before the irreversible op.
2. **OQ2 — M1 dispatch-relation model.** Typed `node_edges.kind='dispatch'` edge (recommended — keeps the node→node relation in the edge store, no nullable FK column) vs an `agents.assigned_node` column (the binding "lives" on the agent)? Affects Task 6.1's migration and every `node_edges` query (a `kind='dep'` filter is required if the typed-edge option is chosen).
3. **OQ3 — `db_topics`/`db_graph` final shape.** After Step 4 both become thin nodes-engine wrappers. Keep them as separate task-tier/topic-tier wrappers, or fold both into a single `dbops/db_nodes.py` (the spec's original name) for one task-engine seam? Recommend keeping `db_graph` as the unified task-node engine and reducing `db_topics` to a thin topic-tier helper; confirm before the Step-4 refactor.
