---
topic: "P8 doctor auto-migration — auto-detect + safely drop legacy tables (CB-P8)"
date: 2026-06-21
spec: specs/2026-06-18-unified-topic-graph.md §P8 / §8 / §12.2 / DA6
audience: planner + coder (reused)
status: facts (READ-ONLY investigation)
tags: [research, migration, doctor, unified-topic-graph]
---

# P8 Doctor Auto-Migration — Facts & Design

> **Goal:** Make `juggle doctor` AUTO-DETECT that a pulled DB still carries the CB-P8 legacy
> tables (`threads`, `graph_topics`, `graph_tasks`, `graph_edges`) and perform the **irreversible**
> drop automatically and safely, for OTHER users who pull the new code and run `doctor` unattended.

## TL;DR / Executive Summary

- Juggle migrations are **presence-based, idempotent guarded functions** — *no version ledger*.
  `run_migrations(conn)` (`src/dbops/migrations.py:22`) is the sole runner, called by
  `JuggleDB.init_db` (`src/juggle_db.py:198`). Each migration self-guards via `PRAGMA table_info` /
  `sqlite_master` checks.
- `doctor` (`src/juggle_cmd_doctor.py:134 cmd_doctor`) is the **sanctioned orchestrator entrypoint**:
  it quiesces the watchdog (`stop_watchdog`, line 217), then calls `JuggleDB(DB_PATH).init_db()`
  (line 223) which runs the whole idempotent migration pass. Legacy-table detection is done by an
  explicit presence check + a `legacy_notes` string printed to the user (lines 186–229).
- **The P8 drop is NOT safe in the current tree** — read-collapse is *incomplete*: `juggle_watchdog.py`,
  `juggle_cmd_threads.py`, `dbops/db_topics.py`, `juggle_cockpit_model.py` (and ~10 more) still read
  `FROM threads` / `FROM graph_topics`. The drop ships ONLY in the same release that finishes read-collapse.
- **Safe predicate = two independent gates:** (1) *static code readiness* — zero source references to
  dropped tables (`doctor --pre-p8-check`, the spec DA6 gate, a CI/dev-time scan); (2) *runtime data
  readiness* — `nodes` exists AND every legacy row is mirrored into `nodes`/`node_edges`
  (anti-join == 0). Both expressible as one testable pure function `p8_drop_ready(conn)`.
- **Backup** before the irreversible drop via the existing `sqlite3.Connection.backup()` pattern
  (`src/juggle_cmd_db_flush.py:49`) → `~/.claude/juggle/juggle.db.bak-pre-p8` (next to the DB,
  mirrors the config-backup convention at `juggle_cmd_doctor.py:152`).
- **G2 boundary:** "agents MUST NOT migrate the shared prod DB" (`assert_migration_allowed`,
  `dbops/graph_guards.py:181`) and "doctor auto-migrates" coexist precisely because doctor runs in
  the **orchestrator** (non-agent) context. The P8 drop must route through the same guard.

---

## 1. Migration Framework

### 1.1 Where migrations live

| Module | Owns | Key fn |
|---|---|---|
| `src/dbops/migrations.py` | Migrations **1–19** inline (legacy `threads` evolution, domain-table drops) + delegates | `run_migrations(conn)` `:22` |
| `src/dbops/migrations_recent.py` | Migrations **20–43**, **44**, **45 (wire)**, **47–49 (wire)** chain | `apply_recent_migrations(conn)` `:30` |
| `src/dbops/migrations_graph.py` | Migrations **35–43** (graph_tasks/topics/edges + node→task rename) + wires 44 | `apply_graph_migrations(conn)` `:173`; `apply_nodes_migration_44` `:162` |
| `src/dbops/migrations_nodes.py` | **Migration 44** impl — create `nodes`/`node_edges` + backfill from old tables | `apply_nodes_migration(conn)` `:37` |
| `src/dbops/migration_selfheal_status_check.py` | **Migration 45** — drop `error_events.status` CHECK (BEGIN IMMEDIATE rebuild) | `migrate_45_drop_status_check(conn)` `:17` |
| `src/dbops/migrations_selfheal_p2.py` | **Migrations 47–49** — group_key + audit + lease | `apply_selfheal_p2_migrations(conn)` |
| `src/dbops/migration_topic_summary_cache.py` | **Migration 46** — additive `topic_summary_cache` (wired in `run_migrations` directly, `migrations.py:279`) | `migrate_46_topic_summary_cache(conn)` |

**Recent migration tail (`migrations_recent.py:300-320`):**
```
40  migrate_runs_vcs(conn)                 # agent_runs VCS cols
41  run_migration_41(conn)                 # drop 4 dead Hindsight cols from threads (table-rebuild)
42  run_migration_slug_wheel(conn)         # juggle_meta + slug-wheel indexes
44  apply_nodes_migration_44(conn)         # nodes + node_edges + backfill (ADDITIVE; old tables stay)
45  _migrate_45_drop_status_check(conn)    # BEGIN IMMEDIATE fail-loud rebuild
47-49 apply_selfheal_p2_migrations(conn)
```
**Next free migration number = 50** (P8 drop = "Migration 50").

### 1.2 The idempotent-guarded-function pattern (no version ledger)

Confirmed in `migrations.py:4-5` and every migration body. There is **no `schema_version` table**;
`doctor` says so explicitly (`juggle_cmd_doctor.py:181` "presence-based; juggle has no schema_version table").
Each migration:
1. Reads current shape: `PRAGMA table_info(<t>)` → `{r[1] for r ...}` or
   `SELECT name FROM sqlite_master WHERE type='table'`.
2. Acts only if needed (`if "<col>" not in cols:` / `if "<t>" not in tables:`).
3. Uses idempotent DDL: `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
   `INSERT OR IGNORE`, `DROP TABLE IF EXISTS`, `ALTER TABLE ... ADD COLUMN` (caught on dup).

There is **no `table_exists()` helper** — the convention is inline `sqlite_master` / `PRAGMA` set
comprehensions (see `migrations_graph.py:27 _tables`, `:36 _cols` for the closest reusable helpers).

### 1.3 Two error conventions (IMPORTANT — they differ)

- **Fail-SOFT (additive / column-add migrations):** wrap in
  `try: ... except sqlite3.OperationalError as e: _log.warning("Migration N ... skipped: %s", e)`.
  A locked/odd DB is swallowed and logged; the pass continues. (e.g. migrations 20–43, 44.)
- **Fail-LOUD (destructive table-rebuild migrations) — spec §6:** acquire the write lock up front
  with `BEGIN IMMEDIATE` and **let a lock error propagate**; `ROLLBACK` on any error so the table is
  never left half-migrated. Canonical example = Migration 45
  (`migration_selfheal_status_check.py:35-69`):
  ```python
  prev_isolation = conn.isolation_level
  conn.isolation_level = None            # explicit txn control
  conn.execute("BEGIN IMMEDIATE")        # take write lock; locked DB RAISES (fail loud)
  try:
      ... RENAME / CREATE / INSERT SELECT / DROP / recreate indexes ...
      conn.execute("COMMIT")
  except Exception:
      conn.execute("ROLLBACK"); raise
  finally:
      conn.isolation_level = prev_isolation
  ```
  **→ The P8 multi-table drop MUST follow this BEGIN IMMEDIATE fail-loud convention.**

### 1.4 The table-rebuild / drop template (Migration 41 + 45)

- **Drop tables outright** (no data kept): Migration 19 pattern (`migrations.py:265-270`) —
  `DROP TABLE IF EXISTS domain_paths; DROP TABLE IF EXISTS domains;` (FK-child first). This is the
  closest precedent for the P8 drop (4 tables, drop edges-referencing first is moot since they're all
  dropped together; order them to satisfy FKs: `graph_edges`, `graph_tasks`, `graph_topics`, `threads`).
- **Drop columns / rebuild** (keep data): Migration 41 pattern (`run_migration_41`,
  `migrations_recent.py:386-443`) — snapshot index SQL from `sqlite_master`, `PRAGMA foreign_keys=OFF`,
  `CREATE TABLE <new>`, `INSERT INTO <new> SELECT ... FROM <old>`, `DROP TABLE <old>`,
  `ALTER TABLE <new> RENAME TO <old>`, replay indexes, `foreign_keys=ON`. (P8 also renames
  `messages.thread_id → node_id` etc. — spec §8.4 step 7 — which uses this rebuild or
  `ALTER ... RENAME COLUMN` via `migrations_graph.py:73 _rename_column`.)

### 1.5 How `doctor` discovers & applies migrations (the wiring)

`doctor` does NOT enumerate migrations. It calls `JuggleDB(DB_PATH).init_db()` (`juggle_cmd_doctor.py:223`),
and `init_db` (`juggle_db.py:129-199`) does `CREATE TABLE IF NOT EXISTS` for the base schema then
`run_migrations(conn)` (`:198`). So **every** migration in the chain auto-applies on any `init_db`.
`doctor`'s only migration-specific logic is *presence detection for the user-facing report*
(`juggle_cmd_doctor.py:186-229`): it reads `threads` cols + table set, sets `legacy_notes` for known
legacy states (domain cols → migrations 17–19; `graph_nodes` → migration 39), and prints them.

> **Design implication:** a NEW migration auto-runs via `init_db`, but to be *surfaced* in doctor's
> output you add a presence-check block in `cmd_doctor` (like the existing `stale` / `node_era` blocks).

### 1.6 Watchdog quiesce that doctor performs (§6, selfheal-v2 P1)

`juggle_cmd_doctor.py:212-226` — before the migration pass (non-dry only):
```python
from juggle_watchdog_singleton import stop_watchdog
if stop_watchdog(DB_PATH):
    print("db: quiesced watchdog for safe migration")   # no-op if none running
... JuggleDB(DB_PATH).init_db() ...
```
Rationale (comment `:213-216`): "quiesce the watchdog before any rebuild so no writer races the
error_events table swap (Migration 45). The 30s backstop relaunches it after migration." **The P8
drop reuses this same quiesce** — and the relaunched watchdog is the *post-collapse* (nodes-only)
binary, satisfying the "cockpit must already be on post-drop code" constraint.

---

## 2. Doctor Command Structure

- **Entry:** `src/juggle_cmd_doctor.py:134 cmd_doctor(args) -> int`.
- **Parser:** `src/juggle_cli_parsers_threads.py:42-48` — subcommand `doctor`, single flag
  `--dry-run` (`store_true`, "Print actions; write nothing"); dispatch
  `func=lambda a: __import__("juggle_cmd_doctor").cmd_doctor(a)`.
- **Steps printed (in order):**
  1. **Config migration** (`:155-179`) — pre-1.21 → 1.21 schema rewrite (`_migrate_config`), backs up to
     `<config>.bak-pre-1.21` before writing.
  2. **Stale-config prune** (`:172-177` → `_check_stale_config`) — every run; prunes inert keys (non-dry).
  3. **DB migration pass** (`:181-232`) — presence detection + `init_db()` (the idempotent pass).
  4. **Graph reconcile** (`:239-260`) — repair task/topic state drift.
  5. **merged_sha backfill** (`:262-279`) — orphan-guard false-positive fix (non-dry only).
  6. **mirror backfill** (`:281-292`) — graph-mirrors-threads (non-dry only).
- **How `--dry-run` is threaded:** `dry = getattr(args, "dry_run", False)` (`:135`). Dry-run is a
  **coarse gate, NOT threaded into the migrations** — `if not dry: ... init_db()` (`:212`), else it
  prints `db: would run <legacy_notes or 'idempotent migration pass'>` (`:227-229`) and **skips the
  whole pass**. Migrations themselves have *no* dry-run mode.
  > **Design implication for P8:** the dry-run *preview* of the drop (table list, row counts, backup
  > path, readiness verdict) must be computed in a **doctor-level block that runs in BOTH dry and
  > non-dry** — you cannot rely on the migration to self-report under dry-run, because the migration
  > never executes in dry mode.

---

## 3. Auto-Detect Design (the safe predicate)

### 3.1 Current state — read-collapse is INCOMPLETE (the drop is NOT yet safe)

Grep of `src/` (excluding migration modules):
- **Still read `FROM threads`:** `juggle_cmd_threads.py`, `juggle_watchdog.py`, `juggle_cli_common.py`,
  `juggle_cmd_projects.py`, `juggle_cockpit_model.py`, `juggle_migrate_lifecycle.py`,
  `schedules/autofix.py`, `schedules/dogfood.py`, `dbops/db_mirror.py`, `dbops/projects.py`,
  `dbops/slug_alloc.py`, `dbops/threads.py`.
- **Still read `FROM graph_topics`:** `juggle_graph_load.py`, `juggle_graph_dispatch*.py`,
  `juggle_cmd_agents_*.py`, `juggle_cockpit_graph_dag.py`, `dbops/db_topics*.py`, `dbops/db_mirror.py`,
  `dbops/orphan_guard.py`.
- **Already read `FROM nodes`:** only `juggle_cockpit_model.py`, `juggle_cockpit_graph_dag.py`,
  `juggle_add_node.py`, `dbops/orphan_guard.py` (these read *both* — mid-migration).

So in this tree the drop would crash the watchdog/cockpit. The auto-drop is correct **only** in the
release whose code is fully nodes-only. That is the "cockpit must already be on post-drop code"
constraint: the drop migration is shipped in the SAME PR as the final read-collapse.

### 3.2 The two-gate safe predicate

**Gate A — static code readiness (`doctor --pre-p8-check`, spec §10 / DA6):** zero source references
to `threads` / `graph_topics` / `graph_tasks` / `graph_edges` remain. Run as a dev/CI grep-or-AST scan
over `src/`; P8 ships only when it shows zero. This is *not* a runtime DB check — it proves the
shipped binary won't query a dropped table. (Recommended belt-and-braces runtime sentinel: a
module-level `P8_READS_COLLAPSED = True` constant flipped in the same PR; doctor refuses to drop if the
running code does not declare it. This guards against a user with a stale checkout.)

**Gate B — runtime data readiness (pure function over the live DB):** the `nodes`/`node_edges` tables
fully mirror the legacy tables, so the drop is lossless. Use an **anti-join == 0** check (robust to the
`INSERT OR IGNORE` + id-collision dedup that migration 44 does — raw COUNT equality is NOT safe because
mirror-topics/dup ids can make counts diverge legitimately):

```python
# AGENT-FIRST testable pure function — proposed home: dbops/migrations_nodes.py
def p8_drop_ready(conn) -> tuple[bool, list[str]]:
    """Return (ready, reasons). ready=True ⟺ legacy tables are present AND every
    legacy row is mirrored into nodes/node_edges (lossless drop). Idempotent:
    once legacy tables are gone it returns (False, ['already-dropped'])."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    legacy = {"threads", "graph_topics", "graph_tasks", "graph_edges"}
    present = legacy & tables
    if not present:
        return False, ["already-dropped"]          # idempotent no-op (re-runnable)
    if "nodes" not in tables or "node_edges" not in tables:
        return False, ["nodes/node_edges missing — migration 44 has not run"]

    reasons = []
    def unmatched(sql):  # count legacy rows with no corresponding node
        return conn.execute(sql).fetchone()[0]

    if "threads" in present and unmatched(
        "SELECT COUNT(*) FROM threads t LEFT JOIN nodes n ON n.id=t.id "
        "WHERE n.id IS NULL"):
        reasons.append("threads rows not mirrored into nodes")
    if "graph_topics" in present and unmatched(
        "SELECT COUNT(*) FROM graph_topics g LEFT JOIN nodes n ON n.id=g.id "
        "WHERE n.id IS NULL"):
        reasons.append("graph_topics rows not mirrored into nodes")
    if "graph_tasks" in present and unmatched(
        "SELECT COUNT(*) FROM graph_tasks g LEFT JOIN nodes n ON n.id=g.id "
        "WHERE n.id IS NULL"):
        reasons.append("graph_tasks rows not mirrored into nodes")
    if "graph_edges" in present and unmatched(
        "SELECT COUNT(*) FROM graph_edges e LEFT JOIN node_edges ne "
        "ON ne.node_id=e.task_id AND ne.depends_on_id=e.depends_on_id "
        "WHERE ne.node_id IS NULL"):
        reasons.append("graph_edges not mirrored into node_edges")

    # spec §8.4 step-8 integrity: no NULL titles, all parent_ids resolvable
    if conn.execute("SELECT 1 FROM nodes WHERE title IS NULL LIMIT 1").fetchone():
        reasons.append("nodes with NULL title")
    if conn.execute("SELECT 1 FROM nodes c WHERE c.parent_id IS NOT NULL AND "
                    "NOT EXISTS (SELECT 1 FROM nodes p WHERE p.id=c.parent_id) "
                    "LIMIT 1").fetchone():
        reasons.append("nodes with unresolvable parent_id")
    return (len(reasons) == 0), reasons
```

**Exposure (AGENT-FIRST):** add `juggle doctor --pre-p8-check [--json]` that prints
`{"ready": bool, "already_dropped": bool, "reasons": [...]}` — a single-shot CLI predicate a test or
agent can assert on without parsing prose. (id-anchored anti-joins are deterministic and unit-testable
on a temp DB.)

### 3.3 Why anti-join, not COUNT — and the matching backfill facts

Migration 44 (`migrations_nodes.py:37-248`) backfills with `INSERT OR IGNORE` and preserves ids
1:1 (`nodes.id == threads.id == graph_topics.id == graph_tasks.id`; mirror-topics become
`kind='conversation'`, real topics `kind='task'`, flat tasks `kind='task'` with `parent_id=topic_id`).
Because ids are preserved, the LEFT JOIN ON `n.id = <legacy>.id` is exact and immune to kind/dup
ambiguity. `node_edges` keys off `(task_id → node_id, depends_on_id)` (`migrations_nodes.py:238-247`).

---

## 4. Safety / Irreversibility for the Auto-Path

The drop is **irreversible** (spec §12.2 `:609`: "After P8 ... rollback requires DB restore from
backup. P8 must be preceded by an explicit DB backup step.").

### 4.1 Per-user automatic backup — WHERE and HOW

- **Where:** next to the DB → `~/.claude/juggle/juggle.db.bak-pre-p8` (DB path resolves via
  `dbops/schema._resolve_db_path()` `:31` = `DEFAULT_DATA_DIR/juggle.db`; `DEFAULT_DATA_DIR` from
  `paths.data_dir`, default `~/.claude/juggle`). This mirrors the existing config-backup convention
  (`juggle_cmd_doctor.py:152` `<config>.bak-pre-1.21`) and lands on the same volume as the DB.
- **How:** the existing consistent-snapshot pattern — `sqlite3.connect(DB).backup(dst)`
  (`juggle_cmd_db_flush.py:46-54`, also `juggle_db_bootstrap.py:30`). A live `.backup()` is a
  transaction-consistent copy (safe even with the watchdog briefly alive), superior to `shutil.copy2`
  of a WAL-mode file.
- **Guard:** `if not backup.exists(): conn.backup(...)` (one-shot, like the config backup). Because the
  drop is idempotent (re-run sees no legacy tables → `already-dropped` → no-op), only the FIRST
  doctor run on a not-yet-migrated DB takes the backup; later runs neither back up nor drop.

### 4.2 Dry-run preview (doctor-level, runs in BOTH dry & non-dry)

```
db(P8): legacy tables present: threads(N), graph_topics(M), graph_tasks(K), graph_edges(E)
db(P8): readiness = READY            # or: BLOCKED — <reasons>
db(P8): would back up → ~/.claude/juggle/juggle.db.bak-pre-p8   (dry-run — no write)
db(P8): would DROP threads, graph_topics, graph_tasks, graph_edges
```
Compute the predicate + counts in the doctor block (it never lives only in the migration, per §2).

### 4.3 Ordering inside doctor (recommended, non-dry)

```
1. assert_migration_allowed(DB_PATH)         # G2 — refuse in agent context (already in init_db)
2. stop_watchdog(DB_PATH)                     # quiesce (existing, juggle_cmd_doctor.py:217)
3. ready, reasons = p8_drop_ready(conn)
4. if ready and not backup.exists():
       sqlite3.connect(DB).backup(open(backup))   # consistent snapshot
5. init_db()  → run_migrations → Migration 50 drop  # guarded by p8_drop_ready + BEGIN IMMEDIATE
6. (30s backstop relaunches the nodes-only watchdog)
```

### 4.4 Migration 50 (the drop) — convention-correct shape

- Lives in its own module (LOC gate), wired into `migrations_recent.py` after 49 (or into
  `run_migrations` directly like migration 46). Guarded at the TOP by `p8_drop_ready(conn)` — returns
  early if not ready or already-dropped (idempotent + re-runnable).
- Uses **BEGIN IMMEDIATE fail-loud** (Migration 45 convention, §1.3) so a locked DB aborts cleanly and
  ROLLBACK leaves all four tables intact — never a half-drop.
- `DROP TABLE IF EXISTS` in FK-safe order (`graph_edges`, `graph_tasks`, `graph_topics`, `threads`),
  plus the `messages.thread_id → node_id` rename (§8.4 step 7) via `_rename_column`
  (`migrations_graph.py:73`) and dropping `schema_graph.py` DDL constants from `init_db`'s base-create
  list (`juggle_db.py:155-157` create `graph_tasks/edges/topics` — these must be removed in the same PR,
  else `init_db` re-creates the tables right after the drop!).

> **Sharp edge:** `init_db` currently re-creates `CREATE_THREADS` / `CREATE_GRAPH_*` *before*
> `run_migrations` (`juggle_db.py:141,155-157`). If Migration 50 drops them inside the same `init_db`
> call, the base-create has *already* re-made them — so the P8 PR must FIRST delete those base
> `CREATE_*` lines from `init_db`, otherwise the drop is undone every run. Verify this ordering in the
> coder task.

### 4.5 Re-runnability & idempotency summary

- Already-dropped → `p8_drop_ready` returns `(False, ['already-dropped'])` → no backup, no drop, no error.
- Backfill incomplete → `(False, [reasons])` → doctor prints BLOCKED + reasons, does NOT drop, does NOT
  back up (nothing destructive happens; user is told what's missing).
- Ready → backup once + drop once, then converges to already-dropped on every subsequent run.

---

## 5. Boundary: "agents MUST NOT migrate prod DB" vs auto-migrating doctor

- **G2 guard:** `dbops/graph_guards.py:181 assert_migration_allowed(db_path)` raises
  `SharedDBMigrationRefused` iff `is_shared_prod_db(db_path)` AND `is_agent_context()`.
  `is_agent_context()` (`:139-164`): `JUGGLE_ORCHESTRATOR=1` wins (always non-agent);
  else `JUGGLE_IS_AGENT=1` / `JUGGLE_AGENT_WORKTREE` / cwd under `juggle-juggle-*` ⇒ agent.
  `SHARED_PROD_DB = ~/.claude/juggle/juggle.db` (`:26`). `init_db` calls it FIRST (`juggle_db.py:138`).
- **Why no conflict:** `doctor` runs in the **orchestrator / human** context (not `JUGGLE_IS_AGENT`,
  not a worktree cwd), so the guard passes and doctor is *allowed* to migrate. Dispatched agents run
  against an isolated DB (or skip migration) and are *refused*. The boundary is **identity, not the
  operation**: only the orchestrator-run doctor migrates the shared DB.
- **Requirement for P8:** the auto-drop must remain behind `assert_migration_allowed` (it already is,
  since it flows through `init_db`). Do NOT add an agent-reachable drop path. The repeated migration
  comments ("DO NOT run against the shared production DB directly; apply via juggle doctor" —
  `migrations_recent.py:302,339,391`; `migrations_nodes.py:10`) codify exactly this: doctor is the
  sanctioned entrypoint; the G2 guard enforces that only the orchestrator's `init_db` ever reaches the
  drop.

---

## 6. Citations (file:line)

| Fact | Location |
|---|---|
| Sole migration runner | `src/dbops/migrations.py:22 run_migrations` |
| `init_db` calls run_migrations | `src/juggle_db.py:198` |
| init_db base-create of legacy tables (must remove for P8) | `src/juggle_db.py:141,155-157` |
| G2 guard called first in init_db | `src/juggle_db.py:138` |
| Recent chain (40,41,42,44,45,47-49) | `src/dbops/migrations_recent.py:300-320` |
| Table-rebuild/drop-column template | `src/dbops/migrations_recent.py:386-443 run_migration_41` |
| Migration 44 backfill (id-preserving) | `src/dbops/migrations_nodes.py:37-248` |
| BEGIN IMMEDIATE fail-loud convention | `src/dbops/migration_selfheal_status_check.py:35-69` |
| Drop-table precedent (FK-child first) | `src/dbops/migrations.py:265-270` |
| `_rename_column` helper | `src/dbops/migrations_graph.py:73` |
| `nodes`/`node_edges` schema | `src/dbops/schema_nodes.py:8-72` |
| doctor entry | `src/juggle_cmd_doctor.py:134 cmd_doctor` |
| doctor DB block + legacy_notes | `src/juggle_cmd_doctor.py:181-232` |
| doctor watchdog quiesce | `src/juggle_cmd_doctor.py:212-226` |
| doctor dry-run is a coarse gate | `src/juggle_cmd_doctor.py:135,212,227-229` |
| config backup convention | `src/juggle_cmd_doctor.py:152,161-168` |
| doctor parser (only --dry-run) | `src/juggle_cli_parsers_threads.py:42-48` |
| DB path resolution | `src/dbops/schema.py:31-39 _resolve_db_path` |
| consistent-backup pattern | `src/juggle_cmd_db_flush.py:46-54`; `src/juggle_db_bootstrap.py:30` |
| G2 predicates + assertion | `src/dbops/graph_guards.py:139-193` |
| spec P8 behavior + done-check | `specs/2026-06-18-unified-topic-graph.md:800-822` |
| spec migration SQL sketch (steps 1-8) | `specs/2026-06-18-unified-topic-graph.md:439-452` |
| spec backup-before-P8 requirement | `specs/2026-06-18-unified-topic-graph.md:609` |
| spec DA6 (irreversible + pre-p8-check) | `specs/2026-06-18-unified-topic-graph.md:837` |

## 7. Open Questions / Risks (for the planner)

- [ ] **Read-collapse must land before/with the drop.** Confirm whether P8's read-collapse is a
  prerequisite PR (recommended) or bundled. The drop migration + `init_db` base-create removal + all
  read-path rewrites + `messages.thread_id` rename should be ONE atomic release.
- [ ] **`init_db` re-create ordering** (§4.4 sharp edge) — base `CREATE_THREADS`/`CREATE_GRAPH_*` lines
  must be deleted in the same PR or the drop is undone every run.
- [ ] **Runtime code sentinel** (`P8_READS_COLLAPSED`) vs trusting the shipped binary — decide whether
  doctor needs a runtime refuse-if-stale guard, or whether `--pre-p8-check` CI gate is sufficient.
- [ ] **Backup retention** — fixed name `juggle.db.bak-pre-p8` (one-shot, simple) vs timestamped
  (multiple). Recommend fixed + `if not exists` (matches config-backup convention).
- [ ] **`db.mode=tmpfs` users** (`juggle_cmd_db_flush.py`) — back up the *durable* path, and ensure the
  drop runs against the live DB then flushes; confirm doctor resolves the durable path for the backup.
