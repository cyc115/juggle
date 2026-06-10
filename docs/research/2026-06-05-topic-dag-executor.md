# Topic DAG Executor — Grounded Design Doc

> **Date:** 2026-06-05  
> **Author:** TF researcher agent  
> **Status:** Draft — for orchestrator review before implementation

---

## 1. Data Model

### New edge table

```sql
CREATE TABLE IF NOT EXISTS topic_dependencies (
  topic_id         TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  depends_on_id    TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (topic_id, depends_on_id)
);
CREATE INDEX IF NOT EXISTS idx_topdep_topic    ON topic_dependencies(topic_id);
CREATE INDEX IF NOT EXISTS idx_topdep_dep      ON topic_dependencies(depends_on_id);
```

**No changes to `threads` schema.** The existing `threads` table (`juggle_db.py:21-42`) already has everything needed:

```
id TEXT PK | status TEXT (active|running|closed|archived) | project_id TEXT | ...
```

Status FSM (`juggle_db.py:919`): `active → running → closed | archived`

The edge table is a pure join table with no payload. A thread is "blocked" if it has ≥1 row in `topic_dependencies` where `depends_on_id` NOT IN (threads with `status='closed'`). A thread is "ready" when that count hits 0.

### DB helper methods to add to `JuggleDB`

```python
def add_dependency(self, topic_id: str, depends_on_id: str) -> None:
    now = _now()
    with self._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO topic_dependencies VALUES (?,?,?)",
            (topic_id, depends_on_id, now),
        )
        conn.commit()

def get_dependencies(self, topic_id: str) -> list[str]:
    """Return list of depends_on_id for a topic."""
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT depends_on_id FROM topic_dependencies WHERE topic_id = ?",
            (topic_id,),
        ).fetchall()
    return [r[0] for r in rows]

def get_ready_blocked_threads(self) -> list[dict]:
    """Return active threads whose every dependency is closed."""
    with self._connect() as conn:
        rows = conn.execute("""
            SELECT t.*
            FROM threads t
            WHERE t.status = 'active'
              AND EXISTS (
                SELECT 1 FROM topic_dependencies WHERE topic_id = t.id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM topic_dependencies d
                JOIN threads dep ON dep.id = d.depends_on_id
                WHERE d.topic_id = t.id
                  AND dep.status != 'closed'
              )
        """).fetchall()
    return [dict(r) for r in rows]

def get_blocked_threads(self) -> list[dict]:
    """Return active threads that have at least one unmet dependency."""
    with self._connect() as conn:
        rows = conn.execute("""
            SELECT DISTINCT t.*
            FROM threads t
            JOIN topic_dependencies d ON d.topic_id = t.id
            JOIN threads dep ON dep.id = d.depends_on_id
            WHERE t.status = 'active'
              AND dep.status != 'closed'
        """).fetchall()
    return [dict(r) for r in rows]
```

---

## 2. Scheduler Loop — The Tick

The DAG tick is **not a background daemon**. It runs synchronously inside `cmd_complete_agent` (`juggle_cmd_agents.py:86`) immediately after step 4 (thread→closed) and step 5 (agent→idle):

```python
# juggle_cmd_agents.py — append to cmd_complete_agent after agent release
def _dag_tick(db: JuggleDB, session_id: str) -> None:
    """Fire once per complete-agent: find newly-unblocked threads, notify."""
    ready = db.get_ready_blocked_threads()
    for thread in ready:
        tid  = thread["id"]
        label = thread.get("title") or thread.get("topic") or tid[:6]
        db.add_notification_v2(
            thread_id=tid,
            message=f"[DAG] '{label}' is now unblocked — all deps closed. Ready to dispatch.",
            session_id=session_id,
        )
        db.add_action_item(
            thread_id=tid,
            message=f"DAG: dispatch '{label}' — deps satisfied",
            type_="decision",
            priority="high",
        )
```

**Why notify + action-item, not auto-dispatch?** See §4 (Integration Collisions) — the task prompt cannot be recovered from thread state alone in MVP. The orchestrator dispatches manually from the action-item.

**Pseudocode for the full tick sequence inside `cmd_complete_agent`:**

```
1. resolve thread_uuid
2. convert open_questions → action_items           (existing step 1)
3. store result_summary as assistant message       (existing step 2)
4. auto-generate summary if empty                  (existing step 3)
5. thread.status → 'closed'                        (existing step 4)
6. agent.status  → 'idle', assigned_thread=None    (existing step 5)
7. add completion notification                     (existing step 5 / cmd)
8. role-based action items                         (existing step 6)
9. _dag_tick(db, session_id)                    ← NEW: unblock dependents
```

The tick is O(threads × deps), well within SQLite's range for Juggle's pool cap of 20 agents / 10 threads.

---

## 3. Dependency Payload — CLI & Storage

### Setting a dependency

```bash
# At thread-creation time
juggle new "Implement caching layer" --depends-on TE --depends-on TF

# Or retroactively
juggle thread TG --add-dep TE --add-dep TF
```

Internally both call `db.add_dependency(topic_id, depends_on_id)` for each dep.

### What is stored

Only the edge: `(topic_id, depends_on_id, created_at)`. No payload, no priority, no weight. The dep edge means: "do not dispatch `topic_id` until `depends_on_id.status == 'closed'`."

### Resolution at dispatch time

The orchestrator (human or future auto-dispatcher) checks:

```python
blocked = db.get_dependencies("TG")  # → ["TE", "TF"]
unmet = [d for d in blocked if db.get_thread(d)["status"] != "closed"]
# if unmet: abort dispatch, print "waiting on: TE, TF"
```

`send-task` (`juggle_cmd_agents.py:694`) should gain a dep-check guard:

```python
# early in cmd_send_task, before pane lookup:
unmet = _unmet_deps(db, thread_uuid)
if unmet and not args.force:
    labels = [db.get_thread(d).get("title") or d[:6] for d in unmet]
    print(f"Error: thread has unmet deps: {', '.join(labels)}. Use --force to override.")
    sys.exit(1)
```

---

## 4. Integration Collisions (Critical)

### 4a. `complete-agent` is the only trigger — watchdog restarts bypass it

`cmd_complete_agent` (`juggle_cmd_agents.py:86`) is the happy-path transition. But the watchdog can kill and restart agents without calling `complete-agent`. A thread restarted by the watchdog does NOT fire `_dag_tick`. **Fix:** add a separate `cmd_mark_closed` subcommand (or call `_dag_tick` from watchdog too) so the tick always fires on thread→closed regardless of path.

Found in `juggle_watchdog_health.py`: `write_heartbeat()` is called on every poll, `is_watchdog_alive()` checks mtime. There is no `set_thread_status('closed')` call in the watchdog path — so the collision is real.

### 4b. Auto-dispatch requires stored task prompts — not available today

`threads` records `last_dispatched_task`, `last_dispatched_role`, `last_dispatched_model` (populated by `send-task`, `juggle_cmd_agents.py:632-634`). This is the LAST dispatched task, not the NEXT one. There is no "pending task" concept. An auto-dispatcher would need a `pending_task TEXT` column on `threads` to know what to send when deps are satisfied. **MVP skips auto-dispatch** and uses action-items instead (see §2).

### 4c. Pool cap race

`MAX_BACKGROUND_AGENTS=20` (`juggle_db.py:16`). `_dag_tick` may unblock many threads simultaneously. If it queues them all, the pool overflows. **Fix:** tick must check idle agent count before queuing:

```python
idle_count = len([a for a in db.get_agents() if a["status"] == "idle"])
ready = db.get_ready_blocked_threads()[:idle_count]  # cap to available agents
```

Even in MVP (notify only), the action-items list could flood the cockpit. Emit at most 3 unblock notifications per tick.

### 4d. Circular dependency — no cycle detection today

`add_dependency` does a blind INSERT. A cycle `A→B→A` would cause `get_ready_blocked_threads` to never return either thread. **Fix:** run a topological-sort check at insert time:

```python
def _would_create_cycle(self, topic_id: str, dep_id: str) -> bool:
    """BFS from dep_id following existing deps; if we reach topic_id, it's a cycle."""
    visited, queue = set(), [dep_id]
    while queue:
        curr = queue.pop()
        if curr == topic_id:
            return True
        if curr in visited:
            continue
        visited.add(curr)
        queue.extend(self.get_dependencies(curr))
    return False
```

Call before `INSERT` in `add_dependency`; raise `ValueError` on cycle.

### 4e. `project_id` auto-assignment

Migration 26 added `project_id` to threads. Dependent threads may span projects. The DAG has no opinion on projects — dep edges cross project boundaries freely. No collision, but cockpit may need to clarify cross-project deps.

### 4f. Context injection is already isolated

`juggle_context.py:161`: `JUGGLE_IS_AGENT=1` agents get only the role anchor — never the orchestrator dashboard. DAG metadata (deps, blocked status) is orchestrator-side only. No risk of agents seeing or acting on DAG state.

---

## 5. Completion Predicate

A thread `T` is **ready** iff:

```sql
SELECT COUNT(*)
FROM topic_dependencies d
JOIN threads dep ON dep.id = d.depends_on_id
WHERE d.topic_id = :T
  AND dep.status != 'closed'
-- result = 0 → ready
```

A thread `T` is **unblocked** iff it was previously blocked (had ≥1 dep) AND that count just dropped to 0 (the trigger being a dependency transitioning to `closed`).

Edge case: a thread with **zero deps** is never blocked — it is immediately dispatchable. `get_ready_blocked_threads` correctly excludes it (the `EXISTS` subquery on `topic_dependencies` filters it out).

---

## 6. Failure Propagation

When a dep thread reaches `archived` (the failure terminal state in `juggle_db.py:919`), its dependents can never unblock under the default predicate (which requires `status='closed'`). Two options:

**Option A (conservative, MVP):** Do nothing. Dep stays blocked forever. The orchestrator gets a stale action-item. When the orchestrator notices the archived thread, they manually resolve: either re-run the dep or add `--force` to dispatch the dependent anyway.

**Option B (cascade):** On `archived` transition, propagate to all dependents:

```python
def _on_dep_archived(db, dep_id, session_id):
    rows = db._connect().execute(
        "SELECT topic_id FROM topic_dependencies WHERE depends_on_id=?", (dep_id,)
    ).fetchall()
    for row in rows:
        tid = row[0]
        db.add_action_item(
            thread_id=tid,
            message=f"DAG: dep '{dep_id[:6]}' FAILED (archived) — decide: re-run dep or --force dispatch",
            type_="decision",
            priority="high",
        )
```

**Recommendation:** Option A for MVP. Option B in v2 once the DAG is validated in production.

---

## 7. The Front-end (Cockpit)

The cockpit currently renders threads in two tiers: Tier 1 (active+running) and Tier 2 (closed+archived), from `juggle_context.py:194-196`. The DAG introduces a "blocked" sub-state within Tier 1.

### Minimal cockpit change (MVP)

Add a "blocked" indicator in the thread status column of the `juggle_context.py` context rendering, not in the Textual TUI (avoids layout churn):

```
# THREADS
TF  active     [blocked: TE,TG]  Topic DAG Executor design doc
TG  active     [ready]           Implement caching layer
TE  closed                       Linux scheduling research
```

`juggle_context.py` builds the active threads list from `db.get_threads_by_status('active')`. Augment that loop:

```python
deps = db.get_dependencies(t["id"])
if deps:
    unmet = [d for d in deps if db.get_thread(d)["status"] != "closed"]
    dep_str = f"[{'blocked: ' + ','.join(u[:6] for u in unmet) if unmet else 'ready'}]"
else:
    dep_str = ""
```

### Future Textual cockpit (v2)

Add a `DependencyGraph` widget in the Thread Detail panel. Show edges as `→` arrows. Highlight blocked threads in amber, ready threads in green.

---

## 8. Prior Art Mapping

| System | Juggle Analogue | Key Difference |
|---|---|---|
| GitHub Actions `needs:` | `topic_dependencies` | GHA has explicit job outputs; Juggle deps are coarse-grained (thread-level) |
| Apache Airflow `depends_on_past` | `_dag_tick` on complete | Airflow has a persistent scheduler daemon; Juggle's tick is ephemeral (on-complete hook) |
| Make `prerequisites` | `get_ready_blocked_threads` | Make reruns on file mtime; Juggle runs on status change |
| Celery `chain` / `chord` | MVP notify → v2 auto-dispatch | Celery auto-dispatches; Juggle MVP delegates to orchestrator |
| GNU Parallel `--halt` | failure propagation §6 | Parallel kills all; Juggle surfaces action-items |

**Closest match:** GitHub Actions `needs:` — declarative dep list, no payload, job-level (not task-level) granularity. Juggle threads ≈ GHA jobs.

---

## 9. MVP vs. Later

### MVP (one migration, ~150 LOC)

1. `topic_dependencies` DDL + index
2. `JuggleDB.add_dependency`, `get_dependencies`, `get_ready_blocked_threads`, `get_blocked_threads`, `_would_create_cycle`
3. `_dag_tick` in `cmd_complete_agent` — notify + action-item only (no auto-dispatch)
4. `--depends-on` flag on `juggle new` and `juggle thread --add-dep`
5. `send-task` dep-check guard with `--force` escape hatch
6. Context rendering: `[blocked: X,Y]` / `[ready]` tags in thread list

**Not in MVP:** auto-dispatch, cockpit graph widget, failure cascade, cross-session DAG persistence across watchdog restarts.

### v2

- Store `pending_task TEXT` on threads so the tick can auto-dispatch without orchestrator
- `DependencyGraph` widget in cockpit Textual TUI
- Failure cascade (Option B from §6)
- `juggle dag show` CLI subcommand — ASCII graph of all thread deps
- DAG-aware watchdog: on thread→closed via watchdog, fire `_dag_tick`

---

## 10. Devil's Advocate

**Is this feature worth adding?**

### Against

1. **Thread count is capped at 10** (`juggle_settings.py:25`: `max_threads: 10`). With ≤10 concurrent threads, manual orchestration is cheap. The human orchestrator can see all threads in the cockpit and dispatch in the right order without a DAG — the DAG saves maybe 2–3 manual dispatches per session.

2. **The killer problem isn't ordering — it's prompt retrieval.** The DAG can unblock a thread but it can't dispatch it without a stored task prompt (§4b). MVP action-items still require a human to read, decide, and dispatch. That's the same cognitive load as manually watching for completion.

3. **Complexity tax is high.** Cycle detection, pool-cap racing, watchdog bypass (§4a), and archived-dep handling (§6) are all real edge cases that must be handled correctly or silently corrupt DB state.

4. **The existing `last_dispatched_task` field is close but wrong.** Repurposing it as `pending_task` is a breaking schema change. A new column is correct but adds migration friction.

### For

1. **Parallel pipelines are the primary use case.** Research → Plan → Implement chains are the exact pattern Juggle users run today, manually. Automating the "watch for completion and then dispatch" step removes a recurring context-switch from the orchestrator.

2. **The edge table is pure additive.** No existing schema changes in MVP. A thread with zero deps behaves identically to today. Rollback = `DROP TABLE topic_dependencies`.

3. **The notify+action-item pattern already exists.** `cmd_complete_agent` already creates notifications and action-items (`juggle_cmd_agents.py:176-210`). `_dag_tick` is just another action-item emitter — it doesn't introduce a new pattern.

### Verdict

**Build MVP.** The edge table + cycle check + `_dag_tick` notify path is ~150 LOC of net-new code with zero existing code changes (except `cmd_complete_agent`). The risk is low, the rollback is clean (`DROP TABLE`), and the value is real for research→plan→implement chains. Skip auto-dispatch until `pending_task` storage is designed separately.

---

## Appendix: Migration Stub

```python
# juggle_db.py — add to MIGRATIONS list
(27, """
CREATE TABLE IF NOT EXISTS topic_dependencies (
  topic_id         TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  depends_on_id    TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (topic_id, depends_on_id)
);
CREATE INDEX IF NOT EXISTS idx_topdep_topic ON topic_dependencies(topic_id);
CREATE INDEX IF NOT EXISTS idx_topdep_dep   ON topic_dependencies(depends_on_id);
"""),
```

Juggle currently uses a numbered migration pattern (migration 26 added `project_id`). This becomes migration 27.
