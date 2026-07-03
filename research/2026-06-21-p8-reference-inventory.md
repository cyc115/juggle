---
topic: "CB-P8 legacy-table drop — current-state reference inventory"
date: 2026-06-21
repo: /Users/mikechen/github/juggle
branch: main
head: 0ee6fd2
spec: specs/2026-06-18-unified-topic-graph.md §P8
supersedes: TODO.md figures dated 2026-06-20 ("~25-29 files / 121 ref-lines", "read-collapse merged 42ef016")
tags: [research, p8, legacy-tables, facts]
---

# CB-P8 Legacy-Table Drop — Reference Inventory (current main)

> **Generated:** 2026-06-21 against `main @ 0ee6fd2`. READ-ONLY audit. Every count below is
> reproducible by the pasted command. Run all commands from the repo root
> `/Users/mikechen/github/juggle`.

## Executive Summary

The TODO's P8 figures are **stale and mislabeled**. Corrections:

1. **`42ef016` is NOT the read-collapse implementation.** It changed only
   `.claude-plugin/plugin.json` + `graphify-out/manifest.json` (a version bump to 1.78.0,
   "P8 read-collapse" *tag*). **Zero `.py` files changed.** The actual nodes work is migration 44
   (`dbops/migrations_nodes.py`) plus partial adoption in 4 modules — see §4.
2. **Reads are NOT collapsed onto `nodes`.** The live cockpit, threads store, projects, schedules,
   etc. still `SELECT … FROM threads/graph_topics/graph_tasks`. Only **21 live ref-lines** touch
   `nodes`, vs **128 live ref-lines** still on the legacy tables.
3. **The legacy surface has not shrunk.** Canonical count today: **161 SQL ref-lines across 37 files**
   (live 128/30; DDL-and-migration-only 33/7). The TODO's "121 / 25-29" was produced by a **broken
   regex** (see §1) and undercounts.
4. **`nodes` is NOT at parity.** It is missing `user_label`, `assigned_by`, and a `last_active_at`
   equivalent; `status→state` needs a **value map**, and `topic/last_active/prompt/topic_id/thread_id`
   need **column renames**. No read is purely mechanical because `nodes` unions all three legacy
   tables and every query must add a `kind`/`parent_id` discriminator. See §2.
5. **No `--pre-p8-check` gate exists.** Proposed reproducible command in §5.

**Bottom line for the planner:** P8 is meaningfully *earlier* than "reads done, just cut writes."
Both reads and writes are still legacy-first in ~26 steady-state modules, and 3 schema gaps must be
closed before any drop is safe.

---

## 1. Reference Inventory — exact current counts

### 1.1 Canonical gate count (the number to track to zero)

Canonical pattern = any SQL statement targeting a legacy table:

```bash
GATE='(FROM|JOIN|INTO|UPDATE|DELETE[[:space:]]+FROM|CREATE[[:space:]]+TABLE([[:space:]]+IF[[:space:]]+NOT[[:space:]]+EXISTS)?|DROP[[:space:]]+TABLE([[:space:]]+IF[[:space:]]+EXISTS)?|REFERENCES)[[:space:]]+(threads|graph_topics|graph_tasks)\b'
grep -rnEi "$GATE" src --include='*.py' | wc -l      # ref-lines
grep -rlEi "$GATE" src --include='*.py' | wc -l      # files
```

| Scope | ref-lines | files |
|-------|-----------|-------|
| **Full `src/` (all categories)** | **161** | **37** |
| Live (excl. `dbops/schema*`, `dbops/migrations*`) | 128 | 30 |
| DDL/migration-only (category c) | 33 | 7 |
| of live: comment/docstring-only (no real SQL) | 3 lines | 3 files |

`grep -rlEi "$GATE" src --include='*.py' | grep -E 'src/dbops/(schema|migrations)'` → the 7 DDL files.

### 1.2 Stale-figure reconciliation (why "121" was wrong)

The TODO command is `grep -rn 'FROM|INTO|UPDATE (threads|graph_topics|graph_tasks)' src/`. That regex
is **mis-grouped** — it matches the bare words `FROM` / `INTO` anywhere, OR `UPDATE <table>`. Outputs:

```bash
# literal TODO command (broken) — matches every line containing "FROM" or "INTO":
grep -rnE 'FROM|INTO|UPDATE (threads|graph_topics|graph_tasks)' src/ --include='*.py' | wc -l   # 324
# corrected FROM/INTO/UPDATE only, properly grouped:
grep -rnEi '(FROM|INTO|UPDATE)[[:space:]]+(threads|graph_topics|graph_tasks)\b' src/ --include='*.py' | wc -l   # 132  (33 files)
```

Neither equals 121; the TODO figure is not reproducible by any clean command and should be replaced
with the canonical **161 / 37** (or live **128 / 30**).

### 1.3 Per-table, per-verb breakdown (full `src/`)

```bash
for t in threads graph_topics graph_tasks; do echo "== $t =="; for v in "FROM" "JOIN" "INTO" "UPDATE" "DELETE FROM" "CREATE TABLE" "DROP TABLE" "REFERENCES"; do n=$(grep -rnEi "${v}( IF (NOT )?EXISTS)?[[:space:]]+${t}\b" src --include='*.py' | wc -l|tr -d ' '); [ "$n" != 0 ] && printf "  %-13s %s\n" "$v" "$n"; done; done
```

| table | FROM | JOIN | INTO | UPDATE | DELETE | CREATE | DROP | REFERENCES |
|-------|-----:|-----:|-----:|-------:|-------:|-------:|-----:|-----------:|
| `threads`      | 47 | 4 | 2 | 18 | 0 | 2 | 1 | 5 |
| `graph_topics` | 16 | 3 | 5 | 10 | 2 | 1 | 0 | 2 |
| `graph_tasks`  | 19 | 8 | 4 | 11 | 0 | 1 | 1 | 2 |

### 1.4 Per-file inventory with category

Categories: **(a) READ** = SELECT/FROM/JOIN only · **(b) WRITE/dual-write** = INSERT/UPDATE/DELETE ·
**(c) DDL/migration** · **(d) test-only**. Files with both reads and writes are marked **a+b**.

#### Live steady-state modules (the drop targets)

| file | tables touched | category | notes |
|------|----------------|----------|-------|
| `src/dbops/threads.py` | threads | a+b | thread store; reads `status`, writes `status/last_active_at/user_label`. **value-map + rename heavy** |
| `src/dbops/db_graph.py` | graph_tasks, graph_edges | a+b | task store; INSERT + UPDATE state/diffstat/handoff/thread_id/title |
| `src/dbops/db_topics.py` | graph_topics, graph_tasks | a+b | topic store; INSERT + UPDATE state/handoff/merged_sha/thread_id |
| `src/dbops/db_mirror.py` | graph_topics, threads | a+b | **mirror projection — DELETE THIS MODULE in nodes model** (see §4) |
| `src/dbops/db_topics_reconcile.py` | graph_topics, graph_tasks | a+b | reconcile; writes `graph_topics.merged_sha` (TODO item 3) |
| `src/dbops/orphan_guard.py` | graph_topics, **nodes** | a+b | **already dual** (reads/writes both graph_topics AND nodes) |
| `src/dbops/projects.py` | threads | a+b | reads `assigned_by`,`topic`,`last_active`; toggles `show_in_list` |
| `src/dbops/slug_alloc.py` | threads | a+b | reads/writes `user_label` |
| `src/dbops/messages.py` | threads | b | `UPDATE threads SET last_active` |
| `src/juggle_add_node.py` | graph_tasks, **nodes** | a+b | **already dual** (docstring: "inserts into nodes and dual-writes to legacy") |
| `src/juggle_cockpit_model.py` | threads, **nodes** | a+b | **5 reads `WHERE status='…'`** (value-map); one `nodes` read |
| `src/juggle_cockpit_graph_dag.py` | graph_topics, graph_tasks, threads, **nodes** | a | **nodes path w/ legacy fallback**; LEFT JOINs threads/graph_topics |
| `src/juggle_cmd_projects.py` | threads | a | reads `topic`, `assigned_by` |
| `src/juggle_cmd_threads.py` | threads | a | reads `topic`, `user_label` |
| `src/juggle_cli_common.py` | threads | a | `SELECT id FROM threads WHERE id LIKE ?` (near-mechanical) |
| `src/juggle_cmd_agents_lifecycle.py` | threads | b | `UPDATE threads SET last_dispatched_*` |
| `src/juggle_watchdog.py` | threads | a+b | reads `status='background'`; writes last_dispatched_* |
| `src/juggle_monitor_daemon.py` | threads | a | `JOIN threads t ON n.thread_id = t.id` |
| `src/juggle_graph_add.py` | graph_tasks | b | `UPDATE graph_tasks SET topic_id` |
| `src/juggle_graph_load.py` | graph_tasks, graph_topics | b | spec loader; UPDATE topic_id/title/objective |
| `src/juggle_graph_dispatch.py` | graph_tasks | a+b | dispatch tick |
| `src/juggle_graph_dispatch_topics.py` | graph_topics | a+b | topic dispatch tick |
| `src/juggle_graph_status.py` | graph_tasks | a | status rollup reads |
| `src/juggle_cockpit.py` | graph_tasks | a | `SELECT prompt, handoff FROM graph_tasks` (prompt→objective) |
| `src/schedules/dogfood.py` | threads | a | reads `topic`, `last_active_at`, `status='active'` |
| `src/schedules/autofix.py` | threads | a | `SELECT id FROM threads ORDER BY created_at` |
| `src/juggle_migrate_lifecycle.py` | threads | b | **one-time migration utility** (not steady-state); UPDATE status |

Comment/docstring-only (no real SQL — exclude from rewrite work):
`src/juggle_cmd_doctor.py:200`, `src/juggle_cmd_graph.py:178`, `src/juggle_cockpit_view.py:77`.

#### Category (c) — DDL / migration files (7)

```bash
grep -rlEi "$GATE" src --include='*.py' | grep -E 'src/dbops/(schema|migrations)'
```

`src/dbops/schema.py` (CREATE_THREADS + FKs), `src/dbops/schema_graph.py` (CREATE_GRAPH_*),
`src/dbops/schema_runs.py`, `src/dbops/migrations.py`, `src/dbops/migrations_graph.py`,
`src/dbops/migrations_nodes.py` (the §P1 backfill), `src/dbops/migrations_recent.py`.
These define/alter the legacy tables and the migration-44 backfill. They are removed/superseded *by*
the P8 drop migration, not before it. **The DROP migration will itself add `DROP TABLE` lines here.**

#### Category (d) — tests (NOT under `src/`; live in `tests/`)

```bash
for t in threads graph_topics graph_tasks; do echo "$t: $(grep -rlEi "$GATE" tests --include='*.py'|grep -c .) files, $(grep -rnEi "$GATE" tests --include='*.py'|wc -l|tr -d ' ') lines"; done
```

| table | test files | SQL ref-lines (tests) |
|-------|-----------:|----------------------:|
| threads | 19 | 72 |
| graph_topics | 10 | 51 |
| graph_tasks | 23 | 56 |

Tests assert against legacy tables directly and will need updating in lockstep with the schema drop
(many seed `threads`/`graph_*` fixtures). Out of scope for the `src/` gate but **must be tracked** —
the drop will red-bar these suites.

---

## 2. `nodes` schema parity vs. live legacy reads

`nodes` DDL: `src/dbops/schema_nodes.py`. **There is ZERO `ALTER TABLE nodes` anywhere** — nodes has
*only* its original columns:

```bash
grep -rnE "ALTER TABLE nodes" src/dbops/*.py   # → no output
```

Authoritative column mapping (from the migration-44 backfill, `dbops/migrations_nodes.py`):

| legacy column | nodes column | transform | mechanical? |
|---------------|--------------|-----------|-------------|
| `threads.id` / `graph_topics.id` / `graph_tasks.id` | `nodes.id` | same UUID (backfill INSERTs `r["id"]`) | ✅ id-join is mechanical |
| `threads.topic` | `nodes.title` | `COALESCE(topic, id)` rename | rewrite |
| `threads.status` | `nodes.state` | **VALUE MAP** `active→open, background→running, running→running, closed→done, failed→failed-exec, done→done, archived→archived` | **rewrite + value translation** |
| `threads.last_active` | `nodes.updated_at` | rename | rewrite |
| `threads.last_user_intent` | `nodes.objective` **and** `nodes.last_user_intent` | duplicated | rewrite |
| `graph_topics.state` / `graph_tasks.state` | `nodes.state` | `pending→open` else identity | rewrite (value) |
| `graph_tasks.prompt` | `nodes.objective` | rename | rewrite |
| `graph_tasks.topic_id` | `nodes.parent_id` | rename | rewrite |
| `graph_topics.thread_id` / `graph_tasks.thread_id` | **(none)** | **dropped — no nodes column** | **blocked / needs new join** |

### 2.1 HARD parity gaps — must be closed BEFORE drop

These columns are read/written by live code (added to `threads` by later migrations, never ported to
`nodes`):

| missing nodes column | added to `threads` by | live consumers | required action |
|----------------------|------------------------|----------------|-----------------|
| **`user_label`** | Migration 14 (`migrations.py:187`) + per-project partial UNIQUE index (`migrations_recent.py:332-361`) | `slug_alloc.py`, `threads.py`, `juggle_cmd_threads.py`, `juggle_migrate_lifecycle.py` | `ALTER TABLE nodes ADD COLUMN user_label TEXT` + replicate the **partial unique index** (must also filter `kind='conversation'`, since nodes unions kinds) |
| **`assigned_by`** | Migration 27 (`migrations_recent.py:159`) `NOT NULL DEFAULT 'auto'` | `projects.py` (`WHERE assigned_by='human'`), `juggle_cmd_projects.py` | `ALTER TABLE nodes ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'auto'` |
| **`last_active_at`** equivalent | Migration 14 (`migrations.py:196`) | cockpit_model, projects, dogfood, threads.py (`ORDER BY last_active_at`) | decide: rewrite reads to `nodes.updated_at`, OR add `last_active_at`. **Note backfill bug:** migration-44 reads `threads.last_active` (the *original* column) not `last_active_at`, so post-migration nodes `updated_at` may be stale for rows that only have `last_active_at`. Flag for the planner. |

No index/FK gap blocks the *task/topic* tiers (`idx_nodes_state/kind/parent/project` exist). The only
index gap is the `user_label` uniqueness constraint above.

### 2.2 The `kind`/`parent_id` discriminator (why "mechanical" is misleading)

`nodes` is a UNION of all three legacy tables, distinguished by:

- `threads` → `nodes WHERE kind='conversation'`
- `graph_topics(is_mirror=0)` → `nodes WHERE kind='task' AND parent_id IS NULL` (topic-tier)
- `graph_topics(is_mirror=1)` → `nodes WHERE kind='conversation'` (was a mirror)
- `graph_tasks` → `nodes WHERE kind='task' AND parent_id = <topic id>` (task-tier sub-node)

⇒ **Every** read must add a `kind` (and often `parent_id`) predicate or it will pull rows that
belonged to a different former table. So even `SELECT id FROM threads` is *near*-mechanical, not
zero-effort: it becomes `SELECT id FROM nodes WHERE kind='conversation'`.

---

## 3. `messages.thread_id` situation (spec RK4)

- The column is **still `thread_id`**; there is **no `node_id` alias** on `messages`:
  ```bash
  grep -nE "thread_id|node_id" src/dbops/messages.py   # all thread_id; zero node_id
  ```
- `messages.thread_id` points at a `nodes.id` value already (since `threads.id == nodes.id` for
  conversation nodes — spec line 854), so **no data migration is needed for the values**, only the
  column *name* is cosmetic until P8.
- **FK blast radius — 4 tables `REFERENCES threads(id)`** (`src/dbops/schema.py`):
  ```bash
  grep -nE "REFERENCES threads" src/dbops/schema.py
  ```
  | line | table | clause |
  |------|-------|--------|
  | 76 | `messages` | `thread_id TEXT NOT NULL REFERENCES threads(id)` |
  | 87 | `notifications` | `thread_id TEXT NOT NULL REFERENCES threads(id)` |
  | 130 | `notifications_v2` | `FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL` |
  | 143 | `action_items` | `FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL` |
- `messages.thread_id` read/write footprint: **18 ref-lines** across messages + notifications code
  (`grep -rnE "thread_id" src --include='*.py' | grep -iE "messages|notifications" | grep -iE "FROM|INTO|UPDATE|REFERENCES|WHERE"`).
- **What's needed for P8:** SQLite cannot `DROP TABLE threads` while these FK clauses exist with
  `PRAGMA foreign_keys=ON`. The repo already uses the table-rebuild idiom with
  `PRAGMA foreign_keys=OFF/ON` (`migrations_recent.py:427-442`). The drop migration must:
  rebuild `messages`/`notifications`/`notifications_v2`/`action_items` to repoint `thread_id → REFERENCES
  nodes(id)` (or drop the FK), optionally rename `thread_id → node_id`, **then** `DROP TABLE threads`.
  Renaming the column is optional/cosmetic and can be deferred; repointing the FK is mandatory.

---

## 4. Ordering — mechanical vs rewrite; writes to cut first

### 4.1 Current nodes adoption (the real "read-collapse" status)

21 live ref-lines touch `nodes` today (vs 128 legacy):

```bash
grep -rnEi "(FROM|INTO|UPDATE|JOIN)[[:space:]]+nodes\b" src --include='*.py' | grep -vE 'src/dbops/(schema|migrations)' | grep -vE 'node_edges|graph_nodes' | wc -l   # 21
```

Modules with a `nodes` path already:
- `juggle_add_node.py` — **true dual-write** (INSERT nodes + INSERT graph_tasks).
- `orphan_guard.py` — **dual** (UPDATE nodes.merged_sha/state AND graph_topics.merged_sha).
- `juggle_cockpit_graph_dag.py` — nodes topic-tier DAG **with legacy fallback** to graph_topics/tasks.
- `juggle_cockpit_model.py` — one `SELECT project_id,state FROM nodes`, but counts still via threads.

Everything else is legacy-only. So the spec's RK5 ("cockpit reads old tables until P8, flip in one
atomic PR") is **half-done in the DAG pane only**; the list/status panes still read `threads`.

### 4.2 Read sites — mechanical (add `kind` filter only)

`id`-keyed / shared-column-name reads — convert by swapping table + adding `kind`:
- `juggle_cli_common.py` `SELECT id FROM threads WHERE id LIKE ?`
- `juggle_monitor_daemon.py` `JOIN threads … ON n.thread_id = t.id` (join key only)
- `db_graph.py` / `db_topics.py` self-joins on `id`/`task_id` (graph_edges → node_edges)
- `juggle_graph_status.py` `SELECT state FROM graph_tasks WHERE project_id=?`

### 4.3 Read sites — REQUIRE rewrite (column rename and/or value map)

- **`cockpit_model.py` — 5× `WHERE status='active'|'archived'|'background'|'closed'|'running'`** →
  `WHERE state='open'|'archived'|'running'|'done'|'running'` (note `active`+`background` *both* exist;
  collapse carefully — `background` and `running` both map to `running`).
- `threads.py` `SELECT status FROM threads`, `WHERE status=?` → state + value map.
- `*.topic` reads → `title` (`cmd_projects`, `cmd_threads`, `threads.py`, `dogfood`).
- `*.prompt` read (`cockpit.py`) → `objective`.
- `*.topic_id` (`graph_tasks`) → `parent_id`.
- `*.thread_id` (graph_topics/graph_tasks) reads (`orphan_guard`, `cockpit_graph_dag`, `db_mirror`,
  `db_topics`) → re-derive via `parent_id`/conversation-node link (no direct column).
- **BLOCKED reads** (no nodes column yet): `user_label` (`slug_alloc`, `threads`, `cmd_threads`),
  `assigned_by` (`projects`, `cmd_projects`), `last_active_at` ordering. → close §2.1 gaps first.

### 4.4 Write sites — cut to nodes-only first (before reads can flip safely)

- **`db_mirror.py` → DELETE THE MODULE.** It maintains `graph_topics(is_mirror=1)` mirror rows so the
  legacy DAG can show conversation threads. In the nodes model, conversations are first-class nodes;
  the mirror is dead. Confirm no remaining caller after the cockpit DAG fully reads `nodes`.
- `juggle_add_node.py` — drop the legacy `INSERT INTO graph_tasks`; keep `INSERT INTO nodes`.
- `orphan_guard.py` — drop `UPDATE graph_topics SET merged_sha`; keep `UPDATE nodes`.
- `db_topics_reconcile.py` — stamp `nodes.merged_sha` only (TODO item 3).
- `db_graph.py` / `db_topics.py` — point all INSERT/UPDATE at `nodes`; route `graph_edges` writes to
  `node_edges`.
- `threads.py`, `slug_alloc.py`, `projects.py`, `messages.py`, `cmd_agents_lifecycle.py`,
  `watchdog.py`, `graph_add.py`, `graph_load.py`, `graph_dispatch*.py` — repoint writes to `nodes`
  (requires §2.1 columns to exist on nodes first).

**Recommended sequence:** (1) close §2.1 schema gaps on `nodes` (migration 50a, additive) →
(2) flip writes to nodes-only per-module, deleting `db_mirror.py` → (3) flip reads (mechanical first,
then value-map rewrites) → (4) repoint messages/notifications FKs (§3) → (5) backup → (6) drop
migration. Reads cannot safely flip before writes, or nodes rows go stale mid-transition.

---

## 5. `--pre-p8-check` gate — does not exist; proposed command

```bash
grep -rniE "pre.?p8.?check|pre_p8" src/ --include='*.py'   # → no output (NOT implemented)
```

No such subcommand exists. Highest migration defined today is **49**
(`grep -rhoE "Migration [0-9]+" src/dbops/*.py | grep -oE '[0-9]+' | sort -n | tail -1`), so the drop
migration is **#50** (gap columns can be #50, drop #51, or combined).

**Proposed reproducible gate** (assert ZERO legacy refs in steady-state code). Tier-1 excludes the
schema/migration modules that legitimately define/drop the tables:

```bash
#!/usr/bin/env bash
# pre-p8-check: fail if any steady-state module still touches a legacy table.
set -euo pipefail
GATE='(FROM|JOIN|INTO|UPDATE|DELETE[[:space:]]+FROM|REFERENCES)[[:space:]]+(threads|graph_topics|graph_tasks)\b'
hits=$(grep -rnEi "$GATE" src --include='*.py' \
        | grep -vE 'src/dbops/(schema|migrations)' \
        | grep -vE ':[0-9]+:\s*#')          # drop comment-only lines
n=$(printf '%s' "$hits" | grep -c . || true)
if [ "$n" -ne 0 ]; then
  echo "PRE-P8 FAIL: $n legacy-table reference(s) remain:"; printf '%s\n' "$hits"; exit 1
fi
echo "PRE-P8 OK: zero steady-state legacy-table references."
```

Today this prints **FAIL: 128** (the live count). Wire it as `juggle doctor --pre-p8-check` (parser
lives in `juggle_cli_parsers_*`; doctor impl `juggle_cmd_doctor.py`) and pin a test that runs it.
A Tier-2 check (post-drop) should assert the only remaining refs are the `DROP TABLE` lines in the
drop migration + removal of the `CREATE_*` constants.

---

## Gaps & Open Questions

- [ ] **Backfill staleness:** migration-44 `_backfill_threads` reads `last_active` not `last_active_at`
      — confirm whether prod `nodes.updated_at` is already stale, and whether a re-backfill is needed
      before flipping reads.
- [ ] **`thread_id` → node relationship:** the spec drops `graph_topics.thread_id`; confirm the
      intended replacement (parent_id chain vs. a conversation-node lookup) for the 4 read sites that
      use it.
- [ ] **`action_items` FK** (`schema.py:143`, `ON DELETE SET NULL`) — confirm its rebuild is in scope
      for the FK repoint alongside messages/notifications/notifications_v2.
- [ ] **Test rewrite cost:** ~52 test files seed/assert legacy tables; the drop will break them.
      Scope a fixtures migration (helper that seeds `nodes` instead) as part of P8, not after.
- [ ] **`is_mirror` column drift:** `CREATE_GRAPH_TOPICS` in `schema_graph.py` lacks `is_mirror`
      (added by Migration 42, `migrations_graph.py:283`). Backfill relies on it; harmless for drop but
      note the constant is not the live schema.

## Recommended Next Steps (for the planner)

1. **Land the gate first** (`juggle doctor --pre-p8-check`, §5) + a pinned test — gives a hard,
   reproducible "zero refs" definition of done. (Spec DA6.)
2. **Migration 50 (additive): nodes parity** — add `user_label` (+ kind-scoped partial unique index),
   `assigned_by`; decide `last_active_at` (rename-reads vs add-column) and fix the backfill-staleness.
3. **Writes → nodes-only, per module**, deleting `db_mirror.py`; cut the two existing dual-writers
   (`add_node`, `orphan_guard`) to nodes-only.
4. **Reads → nodes**, mechanical sites first (§4.2), then value-map/rename rewrites (§4.3); finish the
   cockpit list/status panes the DAG pane already started.
5. **FK repoint** for messages/notifications/notifications_v2/action_items → `nodes(id)` (§3), optional
   `thread_id→node_id` rename.
6. **Backup → drop migration #51** (`DROP TABLE threads/graph_topics/graph_tasks/graph_edges` + remove
   dead `CREATE_*` constants), idempotent, tmp-DB tested, behind the green pre-p8-check.
