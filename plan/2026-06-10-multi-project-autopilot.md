# Multi-Project Parallel Autopilot (3-Tier) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Arm a SET of projects; the watchdog tick fairly dispatches READY **TOPICS** (Project → Topic → Task, R9) across all armed graphs — one agent + one worktree + ONE integrate per topic, tasks as sequential TDD commit units inside it.

**Architecture:** CSV armed set in the existing settings key (zero migration). New `graph_topics` table + `graph_nodes.topic_id` (migration 37 backfills a synthetic single-task topic per flat node — in-flight graphs keep running). Topics reuse the node state machine verbatim (`dbops/db_topics.py` is a thin twin over shared `_TRANSITIONS`). The pure scheduler (`juggle_graph_scheduler.py`) orders ready TOPICS least-loaded-first round-robin. `graph_tick` claims topics and dispatches through the UNCHANGED `_dispatch_via_pool` → `cmd_get_agent`/`cmd_send_task` path; integrate-once-per-topic falls out of integrate-per-thread because the topic owns the thread. Spec: `docs/specs/2026-06-10-multi-project-autopilot.md` (rev 2026-06-11) — read it first, especially §2.2–2.3 and the Devil's Advocate.

**Tech Stack:** Python 3 + pytest + sqlite (`JuggleDB`), Rich/Textual cockpit. Run everything with `uv run`.

**Conventions for every task:**
- Work from repo root. TDD: failing test → SEE it fail → implement → SEE it pass → commit.
- Regression pins name the incident (date + symptom) in their docstring.
- Pre-existing failures on the base commit are not your concern — prove on base, note, move on.
- The tick/dispatch loop is production-critical: NEVER weaken an existing pin in `tests/test_graph_dispatch.py` / `tests/test_graph_contract.py`; adapt assertions only where the surface is renamed (node→topic), keeping the asserted behavior.

---

### Task 0: Preflight — baseline green + WL assumption check

**Files:** none (read-only)

- [ ] **Step 1: Record the baseline**

```bash
uv run pytest -q tests/test_graph_dispatch.py tests/test_cmd_autopilot.py tests/test_graph_status.py tests/test_db_graph.py tests/test_graph_contract.py tests/test_cockpit_graph_dag_load.py 2>&1 | tail -3
```

Expected: all pass. Anything failing HERE on the untouched base is pre-existing — record exact ids in your completion notes and continue.

- [ ] **Step 2: Verify the WL dispatch-visibility assumption**

```bash
git log --grep="visib" --grep="cross-connection" -i --oneline | head -5
```

Expected: a commit for thread WL's dispatch cross-connection-visibility fix. If absent, do NOT block — note it in the completion summary.

---

### Task 1: Armed-set accessors — new module `juggle_autopilot_state.py`

**Files:**
- Create: `src/juggle_autopilot_state.py`
- Modify: `src/juggle_graph_dispatch.py` (replace key constant + `get_armed_project` with re-exports)
- Test: `tests/test_autopilot_state.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for juggle_autopilot_state — CSV armed-set accessors (multi-project
autopilot, 2026-06-10). The settings key autopilot_armed_project remains the
SOLE arming authority (DA M6); its value is now an ordered CSV of project ids
(1-element value ≡ the legacy scalar — zero migration)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import juggle_autopilot_state as st  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "ap.db"))
    d.init_db()
    return d


def test_empty_and_blank_mean_disarmed(db):
    assert st.get_armed_projects(db) == []
    db.set_setting(st.ARMED_PROJECT_KEY, "  ")
    assert st.get_armed_projects(db) == []


def test_legacy_scalar_reads_as_one_element_set(db):
    """REGRESSION PIN (2026-06-10): scalar→set migration. A DB armed by the
    OLD code path (plain scalar set_setting) must read back as a 1-element
    armed set — backward compat is structural, not migratory."""
    db.set_setting(st.ARMED_PROJECT_KEY, "juggle")
    assert st.get_armed_projects(db) == ["juggle"]
    assert st.get_armed_project(db) == "juggle"  # compat shim


def test_csv_parse_strip_dedupe_order(db):
    db.set_setting(st.ARMED_PROJECT_KEY, " a , b ,a,, c ")
    assert st.get_armed_projects(db) == ["a", "b", "c"]


def test_arm_appends_idempotently(db):
    assert st.arm_project(db, "a") == ["a"]
    assert st.arm_project(db, "b") == ["a", "b"]
    assert st.arm_project(db, "a") == ["a", "b"]
    assert db.get_setting(st.ARMED_PROJECT_KEY) == "a,b"


def test_arm_rejects_unsafe_ids(db):
    for bad in ("a,b", "a b", " a", ""):
        with pytest.raises(ValueError):
            st.arm_project(db, bad)
    assert st.get_armed_projects(db) == []


def test_disarm_removes_one_keeps_rest(db):
    st.arm_project(db, "a")
    st.arm_project(db, "b")
    assert st.disarm_project(db, "a") == ["b"]
    assert st.disarm_project(db, "a") == ["b"]  # absent → no-op


def test_set_empty_clears_key(db):
    st.arm_project(db, "a")
    st.set_armed_projects(db, [])
    assert db.get_setting(st.ARMED_PROJECT_KEY) is None


def test_pre_migration_db_degrades_to_empty(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "raw.db"))  # no init_db → no settings table
    assert st.get_armed_projects(d) == []
    assert st.get_armed_project(d) is None
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_autopilot_state.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_autopilot_state'`.

- [ ] **Step 3: Implement `src/juggle_autopilot_state.py`**

```python
"""juggle_autopilot_state — armed-project SET accessors (multi-project autopilot).

Owns: the ``autopilot_armed_project`` settings key (SOLE arming authority,
DA M6) whose value is an ordered CSV of project ids. A 1-element value is
byte-identical to the legacy scalar, so pre-existing DBs need no migration.
Must not own: dispatching, scheduling, or the CLI surface.
"""

from __future__ import annotations

ARMED_PROJECT_KEY = "autopilot_armed_project"


def get_armed_projects(db) -> list[str]:
    """Ordered, deduped armed project ids; [] when disarmed or pre-migration."""
    try:
        raw = db.get_setting(ARMED_PROJECT_KEY) or ""
    except Exception:
        return []  # pre-migration DB without a settings table
    out: list[str] = []
    for part in raw.split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def set_armed_projects(db, pids: list[str]) -> None:
    """Persist the set; empty list clears the key (disarmed)."""
    db.set_setting(ARMED_PROJECT_KEY, ",".join(pids) if pids else None)


def _validate(pid: str) -> None:
    if not pid or pid != pid.strip() or "," in pid or any(c.isspace() for c in pid):
        raise ValueError(
            f"project id {pid!r} is not a valid armed-set member "
            "(no commas/whitespace — ids are slugs)"
        )


def arm_project(db, pid: str) -> list[str]:
    """Append ``pid`` to the armed set (idempotent). Returns the new set."""
    _validate(pid)
    armed = get_armed_projects(db)
    if pid not in armed:
        armed.append(pid)
        set_armed_projects(db, armed)
    return armed


def disarm_project(db, pid: str) -> list[str]:
    """Remove ``pid`` (absent → no-op). Returns the new set."""
    armed = get_armed_projects(db)
    if pid in armed:
        armed.remove(pid)
        set_armed_projects(db, armed)
    return armed


def get_armed_project(db) -> str | None:
    """COMPAT SHIM: first armed project or None (legacy single-armed callers)."""
    armed = get_armed_projects(db)
    return armed[0] if armed else None
```

- [ ] **Step 4: Re-export from `juggle_graph_dispatch`**

Delete `ARMED_PROJECT_KEY = ...` and the `get_armed_project` function from
`src/juggle_graph_dispatch.py`; add with the other imports:

```python
from juggle_autopilot_state import (  # noqa: F401 — re-exported, existing importers
    ARMED_PROJECT_KEY,
    get_armed_project,
    get_armed_projects,
)
```

- [ ] **Step 5: Run new + existing suites**

```bash
uv run pytest -q tests/test_autopilot_state.py tests/test_graph_dispatch.py tests/test_cmd_autopilot.py 2>&1 | tail -3
```

Expected: ALL PASS (re-export keeps existing imports green).

- [ ] **Step 6: Commit**

```bash
git add src/juggle_autopilot_state.py src/juggle_graph_dispatch.py tests/test_autopilot_state.py
git commit -m "feat: armed-project SET accessors (CSV in existing settings key)"
```

---

### Task 2: Schema — `graph_topics` + `graph_nodes.topic_id` + migration 37

**Files:**
- Modify: `src/dbops/schema.py` (add `CREATE_GRAPH_TOPICS`; register in init DDL list)
- Modify: `src/dbops/migrations_recent.py` (migration 37, after migration 36)
- Test: `tests/test_migration_topics.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""Migration 37 — graph_topics backfill (3-tier R9, 2026-06-11).

REGRESSION-CRITICAL: flat graph_nodes (task≡topic) must migrate to synthetic
single-task topics that ADOPT state/thread_id/updated_at so in-flight graphs
keep running (spec DA weakest-item #1: updated_at must be COPIED, not now(),
or the stale-claim sweep timing changes under migration)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "m37.db"))
    d.init_db()  # init_db runs all migrations, incl. 37
    return d


def _flat_node(db, nid, state="pending", thread_id=None, updated_at=None):
    g.create_node(db, node_id=nid, project_id="INBOX", title=f"N {nid}", prompt="p")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET state=?, thread_id=?, topic_id=NULL, "
            "updated_at=COALESCE(?, updated_at) WHERE id=?",
            (state, thread_id, updated_at, nid),
        )
        conn.commit()


def _migrate(db):
    from dbops.migrations_recent import run_recent_migrations
    with db._connect() as conn:
        run_recent_migrations(conn)
        conn.commit()


def test_fresh_db_has_graph_topics_table(db):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_topics'"
        ).fetchone()
    assert row, "graph_topics must exist after init_db"


def test_backfill_wraps_flat_node_in_synthetic_topic(db):
    """REGRESSION PIN (2026-06-11): flat→3-tier. Node state, thread binding,
    and updated_at must be ADOPTED by the synthetic topic."""
    _flat_node(db, "x", state="running", thread_id="th-1",
               updated_at="2026-06-01T00:00:00+00:00")
    _migrate(db)
    with db._connect() as conn:
        node = dict(conn.execute("SELECT * FROM graph_nodes WHERE id='x'").fetchone())
        topic = dict(conn.execute(
            "SELECT * FROM graph_topics WHERE id=?", (node["topic_id"],)
        ).fetchone())
    assert node["topic_id"] == "T-x"
    assert topic["state"] == "running"
    assert topic["thread_id"] == "th-1"
    assert topic["updated_at"] == "2026-06-01T00:00:00+00:00"
    assert topic["project_id"] == "INBOX"


def test_backfill_is_idempotent(db):
    _flat_node(db, "y")
    _migrate(db)
    _migrate(db)  # re-run must not duplicate or error
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM graph_topics WHERE id='T-y'").fetchone()[0]
    assert n == 1


def test_backfill_collision_uses_alternate_id(db):
    """A node literally named 'T-z' must not abort the migration when node 'z'
    also exists — collision falls back to 'T#z' (spec DA weakest-item #4)."""
    _flat_node(db, "T-z")
    _flat_node(db, "z")
    _migrate(db)
    with db._connect() as conn:
        tz = conn.execute(
            "SELECT topic_id FROM graph_nodes WHERE id='z'").fetchone()[0]
        topics = {r[0] for r in conn.execute("SELECT id FROM graph_topics")}
    assert tz == "T#z"
    assert len(topics) == 2, "both nodes wrapped despite the name collision"
```

Note: if `run_recent_migrations` is not the real entrypoint name, find it
(`grep -n "^def " src/dbops/migrations_recent.py`) and adapt `_migrate` —
intent unchanged.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_migration_topics.py -v
```

Expected: FAIL — `no such table: graph_topics` / `no such column: topic_id`.

- [ ] **Step 3: Implement**

In `src/dbops/schema.py`, after `CREATE_GRAPH_EDGES`:

```python
# 3-tier hierarchy (R9, 2026-06-11): a Topic owns a task-DAG; ONE thread/agent/
# worktree per topic; integrate runs once per topic. Topics reuse the node
# state machine (dbops.db_topics imports db_graph._TRANSITIONS).
CREATE_GRAPH_TOPICS = """
CREATE TABLE IF NOT EXISTS graph_topics (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  title       TEXT NOT NULL,
  objective   TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'pending',
  thread_id   TEXT,
  handoff     TEXT,
  diffstat    TEXT,
  verified_at TEXT,
  created_at  TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
```

Find where `CREATE_GRAPH_NODES` is executed at init
(`grep -rn "CREATE_GRAPH_NODES" src/dbops/`) and execute `CREATE_GRAPH_TOPICS`
in the same list. In `src/dbops/migrations_recent.py`, append after
migration 36 (same try/skip pattern; import `CREATE_GRAPH_TOPICS` alongside the
existing schema imports):

```python
    # Migration 37: graph_topics + graph_nodes.topic_id (3-tier R9, 2026-06-11).
    # Backfill wraps each flat node in a synthetic single-task topic ADOPTING
    # state/thread_id/updated_at — in-flight graphs keep running, and the
    # stale-sweep clock is preserved (updated_at copied, never now()).
    try:
        conn.execute(CREATE_GRAPH_TOPICS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_topics_project_state "
            "ON graph_topics(project_id, state)"
        )
        try:
            conn.execute(
                "ALTER TABLE graph_nodes ADD COLUMN topic_id TEXT "
                "REFERENCES graph_topics(id)"
            )
        except sqlite3.OperationalError:
            pass  # column exists — idempotent re-run
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_topic "
            "ON graph_nodes(topic_id)"
        )
        node_ids = {r[0] for r in conn.execute("SELECT id FROM graph_nodes")}
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE topic_id IS NULL"
        ).fetchall()
        for n in rows:
            tid = f"T-{n['id']}"
            if tid in node_ids:  # node literally named 'T-<x>' exists
                tid = f"T#{n['id']}"
            conn.execute(
                "INSERT OR IGNORE INTO graph_topics (id, project_id, title, "
                "objective, state, thread_id, handoff, diffstat, verified_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, n["project_id"], n["title"], "", n["state"],
                 n["thread_id"], n["handoff"], n["diffstat"], n["verified_at"],
                 n["created_at"], n["updated_at"]),
            )
            conn.execute(
                "UPDATE graph_nodes SET topic_id=? WHERE id=?", (tid, n["id"])
            )
        conn.commit()
        _log.info("Migration 37: graph_topics created, %d node(s) backfilled",
                  len(rows))
    except sqlite3.OperationalError as e:
        _log.warning("Migration 37 (graph_topics) skipped: %s", e)
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest -q tests/test_migration_topics.py tests/test_db_graph.py -v 2>&1 | tail -4
```

Expected: ALL PASS (existing db_graph tests untouched — `topic_id` is additive).

- [ ] **Step 5: Commit**

```bash
git add src/dbops/schema.py src/dbops/migrations_recent.py tests/test_migration_topics.py
git commit -m "feat: graph_topics table + topic_id + migration 37 backfill (R9 3-tier)"
```

---

### Task 3: Topic store — `src/dbops/db_topics.py`

**Files:**
- Create: `src/dbops/db_topics.py`
- Test: `tests/test_db_topics.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""dbops.db_topics — topic CRUD, shared state machine, DERIVED topic deps
(task edges crossing topic boundaries), ready-set, completion (R9 3-tier)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "topics.db"))
    d.init_db()
    return d


def _topic(db, tid, project="INBOX", **kw):
    t.create_topic(db, topic_id=tid, project_id=project,
                   title=kw.get("title", f"Topic {tid}"),
                   objective=kw.get("objective", ""))


def _task(db, nid, topic_id, deps=()):
    g.create_node(db, node_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    with db._connect() as conn:
        conn.execute("UPDATE graph_nodes SET topic_id=? WHERE id=?", (topic_id, nid))
        conn.commit()
    if deps:
        g.replace_edges(db, nid, list(deps))


def test_topic_uses_node_state_machine(db):
    _topic(db, "ta")
    assert t.get_topic(db, "ta")["state"] == "pending"
    assert t.topic_transition(db, "ta", "deps_ready") == "ready"
    assert t.topic_transition(db, "ta", "claim") == "dispatching"
    with pytest.raises(ValueError):
        t.topic_transition(db, "ta", "integrate_ok")  # illegal from dispatching


def test_derived_topic_deps_from_cross_topic_task_edges(db):
    """Topic A depends on topic B iff any task of A has an edge to a task of B.
    Intra-topic edges must NOT create a self-dep."""
    _topic(db, "A"); _topic(db, "B")
    _task(db, "b1", "B")
    _task(db, "a1", "A")
    _task(db, "a2", "A", deps=("a1", "b1"))  # intra (a1) + cross (b1)
    assert t.derived_topic_deps(db, "A") == ["B"]
    assert t.derived_topic_deps(db, "B") == []


def test_topic_ready_requires_dep_topics_verified(db):
    _topic(db, "A"); _topic(db, "B")
    _task(db, "b1", "B")
    _task(db, "a1", "A", deps=("b1",))
    assert t.recompute_topic_ready(db, "INBOX") == ["B"]  # A blocked on B
    assert t.get_topic(db, "A")["state"] == "pending"
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        t.topic_transition(db, "B", ev)
    assert t.recompute_topic_ready(db, "INBOX") == ["A"]


def test_list_topic_tasks_topological_order(db):
    """The agent executes tasks sequentially in intra-topic dependency order."""
    _topic(db, "A")
    _task(db, "a1", "A")
    _task(db, "a3", "A", deps=("a2",))
    _task(db, "a2", "A", deps=("a1",))
    assert [n["id"] for n in t.list_topic_tasks(db, "A")] == ["a1", "a2", "a3"]


def test_mark_topic_completion_maps_outcomes(db):
    _topic(db, "A")
    for ev in ("deps_ready", "claim", "dispatch"):
        t.topic_transition(db, "A", ev)
    state = t.mark_topic_completion(db, "A", integrate_ok=True, verify_ok=True,
                                    handoff="done")
    assert state == "verified"
    assert t.get_topic(db, "A")["handoff"] == "done"


def test_topic_counts_shape(db):
    _topic(db, "A"); _topic(db, "B")
    c = t.topic_counts(db, "INBOX")
    assert c["total"] == 2 and c["pending"] == 2
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_db_topics.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'dbops.db_topics'`.

- [ ] **Step 3: Implement `src/dbops/db_topics.py`**

```python
"""dbops.db_topics — graph_topics store (3-tier autopilot, R9 2026-06-11).

Owns: topic CRUD, topic state transitions (REUSES db_graph's _TRANSITIONS —
one state machine, two tables, no second invention), DERIVED topic-level deps
(task edges crossing topic boundaries), the topic ready-set (CAS promote), and
topic completion marking.
Must not own: task semantics (dbops.db_graph), dispatching
(juggle_graph_dispatch — whose atomic topic claim is the sanctioned writer
besides topic_transition), CLI parsing.
"""

from __future__ import annotations

from dbops.schema import _now
from dbops.db_graph import _EVENTS, _TRANSITIONS, _cx


def topic_transition(db, topic_id: str, event: str, conn=None) -> str:
    """Apply ``event`` to the topic. Same machine as nodes. Fail-loud."""
    if event not in _EVENTS:
        raise ValueError(f"graph topic event unknown: {event!r}")
    topic = get_topic(db, topic_id, conn=conn)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    key = (topic["state"], event)
    if key not in _TRANSITIONS:
        raise ValueError(
            f"illegal graph transition: topic {topic_id!r} in state "
            f"{topic['state']!r} got event {event!r}"
        )
    new_state = _TRANSITIONS[key]
    now = _now()
    sets, params = ["state=?", "updated_at=?"], [new_state, now]
    if new_state == "verified":
        sets.append("verified_at=?")
        params.append(now)
    if event == "reload":
        sets.append("thread_id=NULL")
    with _cx(db, conn) as c:
        c.execute(
            f"UPDATE graph_topics SET {', '.join(sets)} WHERE id=?",
            (*params, topic_id),
        )
    return new_state


def create_topic(db, *, topic_id, project_id, title, objective="", conn=None) -> None:
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "INSERT INTO graph_topics (id, project_id, title, objective, state, "
            "created_at, updated_at) VALUES (?,?,?,?, 'pending', ?, ?)",
            (topic_id, project_id, title, objective, now, now),
        )


def get_topic(db, topic_id, conn=None) -> dict | None:
    with _cx(db, conn) as c:
        row = c.execute("SELECT * FROM graph_topics WHERE id=?", (topic_id,)).fetchone()
        return dict(row) if row else None


def get_topic_by_thread(db, thread_id) -> dict | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT * FROM graph_topics WHERE thread_id=?", (thread_id,)
        ).fetchone()
    return dict(row) if row else None


def list_topics(db, project_id) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_topics WHERE project_id=? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_topic_thread(db, topic_id, thread_id) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_topics SET thread_id=?, updated_at=? WHERE id=?",
            (thread_id, _now(), topic_id),
        )
        conn.commit()


def set_topic_handoff(db, topic_id, handoff) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_topics SET handoff=?, updated_at=? WHERE id=?",
            (handoff, _now(), topic_id),
        )
        conn.commit()


def list_topic_tasks(db, topic_id) -> list[dict]:
    """Tasks of a topic in intra-topic topological order (created_at,id ties).

    The topic agent executes tasks SEQUENTIALLY in this order (R9 hybrid)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM graph_nodes WHERE topic_id=? ORDER BY created_at, id",
            (topic_id,),
        ).fetchall()
    tasks = [dict(r) for r in rows]
    ids = {n["id"] for n in tasks}
    if not ids:
        return []
    with db._connect() as conn:
        edges = conn.execute(
            "SELECT node_id, depends_on_id FROM graph_edges "
            "WHERE node_id IN (%s)" % ",".join("?" * len(ids)),
            tuple(ids),
        ).fetchall()
    deps = {n["id"]: set() for n in tasks}
    for e in edges:
        if e["depends_on_id"] in ids:  # intra-topic edges only order execution
            deps[e["node_id"]].add(e["depends_on_id"])
    ordered, emitted = [], set()
    pool = list(tasks)
    while pool:
        progressed = False
        for n in list(pool):
            if deps[n["id"]] <= emitted:
                ordered.append(n)
                emitted.add(n["id"])
                pool.remove(n)
                progressed = True
        if not progressed:  # cycle — load-time validation should prevent this
            ordered.extend(pool)
            break
    return ordered


def derived_topic_deps(db, topic_id) -> list[str]:
    """Topics this topic depends on: any task edge crossing the boundary."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT d.topic_id FROM graph_edges e "
            "JOIN graph_nodes n ON n.id = e.node_id "
            "JOIN graph_nodes d ON d.id = e.depends_on_id "
            "WHERE n.topic_id=? AND d.topic_id IS NOT NULL AND d.topic_id != ? "
            "ORDER BY d.topic_id",
            (topic_id, topic_id),
        ).fetchall()
    return [r[0] for r in rows]


def topic_ready_eligible(db, project_id) -> list[str]:
    """Pending topics whose DERIVED dep topics are all 'verified'."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT t.id FROM graph_topics t WHERE t.project_id=? "
            "AND t.state='pending' AND NOT EXISTS ("
            "  SELECT 1 FROM graph_edges e"
            "  JOIN graph_nodes n ON n.id = e.node_id"
            "  JOIN graph_nodes d ON d.id = e.depends_on_id"
            "  JOIN graph_topics dt ON dt.id = d.topic_id"
            "  WHERE n.topic_id = t.id AND d.topic_id != t.id"
            "  AND dt.state != 'verified') "
            "ORDER BY t.created_at, t.id",
            (project_id,),
        ).fetchall()
        return [r["id"] for r in rows]


def recompute_topic_ready(db, project_id) -> list[str]:
    """CAS-promote eligible pending topics to 'ready' (same race discipline as
    db_graph.recompute_ready — a lost race is a silent no-op)."""
    newly = []
    for tid in topic_ready_eligible(db, project_id):
        with _cx(db) as conn:
            cur = conn.execute(
                "UPDATE graph_topics SET state='ready', updated_at=? "
                "WHERE id=? AND state='pending'",
                (_now(), tid),
            )
        if cur.rowcount == 1:
            newly.append(tid)
    return newly


_ADVANCE_TO_INTEGRATING = {
    "pending": ("deps_ready", "claim", "dispatch", "integrate_start"),
    "ready": ("claim", "dispatch", "integrate_start"),
    "dispatching": ("dispatch", "integrate_start"),
    "running": ("integrate_start",),
    "integrating": (),
}


def mark_topic_completion(db, topic_id, *, integrate_ok, verify_ok=True,
                          handoff=None) -> str:
    """Topic twin of db_graph.mark_completion: walk legally to 'integrating',
    apply the outcome. verified-means-MERGED holds at topic level (spec §2.3)."""
    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    if topic["state"] not in _ADVANCE_TO_INTEGRATING:
        raise ValueError(
            f"cannot mark completion: topic {topic_id!r} in terminal state "
            f"{topic['state']!r}"
        )
    if handoff is not None:
        set_topic_handoff(db, topic_id, handoff)
    for event in _ADVANCE_TO_INTEGRATING[topic["state"]]:
        topic_transition(db, topic_id, event)
    if not integrate_ok:
        return topic_transition(db, topic_id, "integrate_fail")
    if not verify_ok:
        return topic_transition(db, topic_id, "verify_fail")
    return topic_transition(db, topic_id, "integrate_ok")


def mark_topic_exec_failed(db, topic_id) -> str:
    """Agent death / give-up: walk the topic legally to 'failed-exec'
    (mirror of db_graph.mark_exec_failed — read it and follow its walk)."""
    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")
    walk = {"pending": ("deps_ready", "claim", "dispatch"),
            "ready": ("claim", "dispatch"),
            "dispatching": ("dispatch",),
            "running": ()}
    if topic["state"] not in walk:
        raise ValueError(
            f"cannot mark exec-failed: topic {topic_id!r} in state "
            f"{topic['state']!r}"
        )
    for event in walk[topic["state"]]:
        topic_transition(db, topic_id, event)
    return topic_transition(db, topic_id, "exec_fail")


def propagate_topic_failure(db, topic_id) -> list[str]:
    """Block transitive DERIVED dependents of a failed topic (blocked-failed).
    Mirror of db_graph.propagate_failure over derived topic deps."""
    blocked: list[str] = []
    frontier = [topic_id]
    while frontier:
        cur = frontier.pop()
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT n.topic_id FROM graph_edges e "
                "JOIN graph_nodes n ON n.id = e.node_id "
                "JOIN graph_nodes d ON d.id = e.depends_on_id "
                "WHERE d.topic_id=? AND n.topic_id != ?",
                (cur, cur),
            ).fetchall()
        for r in rows:
            dep_tid = r[0]
            t_ = get_topic(db, dep_tid)
            if t_ and t_["state"] in ("pending", "ready"):
                topic_transition(db, dep_tid, "dep_fail")
                blocked.append(dep_tid)
                frontier.append(dep_tid)
    return blocked


def topic_counts(db, project_id) -> dict | None:
    """Display counts over graph_topics (same shape as juggle_graph_status)."""
    from juggle_graph_status import counts_from_states

    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT state FROM graph_topics WHERE project_id=?", (project_id,)
            ).fetchall()
    except Exception:
        return None  # pre-migration DB
    states = [r[0] for r in rows]
    return counts_from_states(states) if states else None
```

NOTE: `_TRANSITIONS`/`_EVENTS`/`_cx` are module-private in `db_graph` — in-repo
reuse is intentional (one machine, spec §2.2). Do NOT copy the dict.

- [ ] **Step 4: Run tests**

```bash
uv run pytest -q tests/test_db_topics.py tests/test_db_graph.py -v 2>&1 | tail -4
```

Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/dbops/db_topics.py tests/test_db_topics.py
git commit -m "feat: topic store — shared state machine, derived deps, ready-set (R9)"
```

---

### Task 4: Spec format — topic tier + legacy fallback; `add-node --topic`

**Files:**
- Modify: `src/juggle_graph_upsert.py` (parse + validate topics)
- Modify: `src/juggle_cmd_graph.py` (load creates topics; `add-node --topic`)
- Test: `tests/test_graph_spec_topics.py` (new); existing `tests/test_cmd_graph.py` must stay green

- [ ] **Step 1: Write the failing tests**

```python
"""Topic-tier graph spec parsing/loading (R9). Legacy flat specs must load
unchanged as synthetic single-task topics (R6)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_upsert import parse_topics_spec, validate_topics  # noqa: E402

TOPIC_SPEC = """\
## topic auth: Authentication
Build login end-to-end.

### t1: DB schema
verify_cmd: pytest tests -q
Create users table.

### t2: Login endpoint
deps: t1
Implement /login.

## topic ui: Frontend
### u1: Login form
deps: t2
Render the form.
"""

LEGACY_SPEC = """\
## n1: First
Do the first thing.

## n2: Second
deps: n1
Do the second thing.
"""


def test_parse_topics_spec_two_tiers():
    topics = parse_topics_spec(TOPIC_SPEC)
    assert [t["id"] for t in topics] == ["auth", "ui"]
    assert topics[0]["objective"].startswith("Build login")
    assert [n["id"] for n in topics[0]["tasks"]] == ["t1", "t2"]
    assert topics[0]["tasks"][1]["deps"] == ["t1"]
    assert topics[1]["tasks"][0]["deps"] == ["t2"]  # cross-topic task dep


def test_legacy_flat_spec_wraps_each_node_in_synthetic_topic():
    """REGRESSION PIN (2026-06-11 R6): existing flat spec files must keep
    loading — each old `## node` becomes a 1-task topic (task ≡ topic)."""
    topics = parse_topics_spec(LEGACY_SPEC)
    assert [t["id"] for t in topics] == ["T-n1", "T-n2"]
    assert all(len(t["tasks"]) == 1 for t in topics)
    assert topics[1]["tasks"][0]["deps"] == ["n1"]


def test_mixed_spec_rejected():
    mixed = TOPIC_SPEC + "\n## stray: Flat node\nprompt\n"
    errors = validate_topics(parse_topics_spec(mixed))
    assert any("mix" in e.lower() for e in errors)


def test_empty_topic_rejected():
    errors = validate_topics(parse_topics_spec("## topic empty: Nothing\nobjective only\n"))
    assert any("no tasks" in e.lower() for e in errors)


def test_cross_topic_cycle_rejected():
    spec = """\
## topic A: a
### a1: x
deps: b1
p
## topic B: b
### b1: y
deps: a1
p
"""
    errors = validate_topics(parse_topics_spec(spec))
    assert any("cycle" in e.lower() for e in errors)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_spec_topics.py -v
```

Expected: FAIL — `ImportError: cannot import name 'parse_topics_spec'`.

- [ ] **Step 3: Implement in `src/juggle_graph_upsert.py`**

Add (keep `parse_graph_spec`/`validate_graph` untouched — legacy callers and
tests use them):

```python
_TOPIC_HEADING_RE = re.compile(r"^##\s+topic\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")
_TASK_HEADING_RE = re.compile(r"^###\s+([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$")


def parse_topics_spec(text: str) -> list[dict]:
    """Parse a 3-tier spec: [{'id','title','objective','tasks':[node dicts]}].

    LEGACY FALLBACK (R6): a spec with no `## topic` headings parses via
    parse_graph_spec and wraps each flat node in a synthetic 1-task topic
    'T-<id>' — the exact shape migration 37 produces. A spec mixing both
    heading forms gets a '_mixed' marker for validate_topics to reject
    (parse never raises; validation reports).
    """
    if not any(_TOPIC_HEADING_RE.match(l) for l in text.splitlines()):
        return [
            {"id": f"T-{n['id']}", "title": n["title"], "objective": "",
             "tasks": [n]}
            for n in parse_graph_spec(text)
        ]
    topics: list[dict] = []
    current_topic: dict | None = None
    current_task: dict | None = None
    body: list[str] = []
    obj: list[str] = []

    def _flush_task():
        nonlocal current_task
        if current_task is not None:
            current_task["prompt"] = "\n".join(body).strip()
            current_topic["tasks"].append(current_task)
            current_task = None

    def _flush_topic():
        nonlocal current_topic
        if current_topic is not None:
            _flush_task()
            current_topic["objective"] = "\n".join(obj).strip()
            topics.append(current_topic)
            current_topic = None

    for line in text.splitlines():
        tm = _TOPIC_HEADING_RE.match(line)
        if tm:
            _flush_topic()
            current_topic = {"id": tm.group(1), "title": tm.group(2), "tasks": []}
            obj, body = [], []
            continue
        if current_topic is None:
            continue  # preamble
        if _HEADING_RE.match(line):
            # flat `## x:` heading inside a topic spec — mixed form, reject later
            current_topic["_mixed"] = True
            continue
        km = _TASK_HEADING_RE.match(line)
        if km:
            _flush_task()
            current_task = {"id": km.group(1), "title": km.group(2),
                            "deps": [], "verify_cmd": None}
            body = []
            continue
        fm = _FIELD_RE.match(line)
        if fm and current_task is not None:
            field, value = fm.group(1), fm.group(2).strip()
            if field == "deps":
                current_task["deps"] = [d.strip() for d in value.split(",") if d.strip()]
            else:
                current_task["verify_cmd"] = value or None
            continue
        (body if current_task is not None else obj).append(line)
    _flush_topic()
    return topics


def validate_topics(topics: list[dict]) -> list[str]:
    """Validation across both tiers. Reuses validate_graph for the task tier,
    then: mixed form, empty topics, duplicate topic ids, and a cycle in the
    DERIVED topic deps."""
    errors: list[str] = []
    if any(t.get("_mixed") for t in topics):
        errors.append("spec mixes `## topic` and flat `## node` headings — pick one form")
    tids = [t["id"] for t in topics]
    seen: set[str] = set()
    for tid in tids:
        if tid in seen:
            errors.append(f"duplicate topic id: {tid!r}")
        seen.add(tid)
    for t in topics:
        if not t["tasks"]:
            errors.append(f"topic {t['id']!r} has no tasks — it can never complete")
    all_tasks = [n for t in topics for n in t["tasks"]]
    errors += validate_graph(all_tasks) if all_tasks else ["spec has no tasks"]
    owner = {n["id"]: t["id"] for t in topics for n in t["tasks"]}
    tedges = sorted({
        (owner[n["id"]], owner[d])
        for t in topics for n in t["tasks"] for d in n["deps"]
        if d in owner and owner[d] != owner[n["id"]]
    })
    if not errors:
        cyc = find_cycle(tids, tedges)
        if cyc:
            errors.append(f"topic dependency cycle involving: {', '.join(cyc)}")
    return errors
```

NOTE on `_HEADING_RE` inside topic specs: `## topic x: T` ALSO matches the
legacy `_HEADING_RE` (id="topic"). Order the checks exactly as above — the
topic regex is tested FIRST and `continue`s, so only NON-topic `##` headings
set `_mixed`.

- [ ] **Step 4: Wire `project-graph load` + `add-node --topic`**

In `src/juggle_cmd_graph.py` `cmd_project_graph_load`: parse with
`parse_topics_spec` + `validate_topics`; inside the existing one-transaction
upsert loop, for each topic call `db_topics.create_topic` if missing (guarded:
a topic in `PROTECTED_STATES` keeps its state; title/objective may update),
then upsert its tasks via the EXISTING node path, additionally setting
`graph_nodes.topic_id` (extend the node upsert helper with a `topic_id=`
parameter, default None). For `cmd_graph_add_node`: add `--topic <id>`; when
the project has any non-synthetic topic, `--topic` is REQUIRED (stderr +
exit 1); when omitted on a flat project, auto-create synthetic topic
`T-<node-id>` (today's behavior preserved). Read the existing functions first
and follow their structure — behavior contract above, mechanics theirs.

- [ ] **Step 5: Run suites + commit**

```bash
uv run pytest -q tests/test_graph_spec_topics.py tests/test_cmd_graph.py tests/test_graph_add_node.py -v 2>&1 | tail -4
git add src/juggle_graph_upsert.py src/juggle_cmd_graph.py tests/test_graph_spec_topics.py
git commit -m "feat: topic-tier graph spec format with legacy flat fallback (R9/R6)"
```

Expected: ALL PASS — legacy spec tests in `test_cmd_graph.py` are the R6 pins.

---

### Task 5: Fair scheduler — pure `juggle_graph_scheduler.py` (topic-level)

**Files:**
- Create: `src/juggle_graph_scheduler.py`
- Test: `tests/test_graph_scheduler.py` (new)

The function is tier-agnostic (orders opaque dicts); it is FED topics (spec
§2.7). Tests phrase fixtures as topics.

- [ ] **Step 1: Write the failing tests**

```python
"""juggle_graph_scheduler — least-loaded-first round-robin interleave over
ready TOPICS (R3/R9, spec §2.7). Pure function, no DB. The topic is the
budget unit: one topic = one thread = one agent."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_scheduler import interleave_ready  # noqa: E402


def _t(i):
    return {"id": i}


def test_empty_input():
    assert interleave_ready({}, {}, []) == []


def test_single_project_order_preserved():
    ready = {"p1": [_t("a"), _t("b")]}
    assert interleave_ready(ready, {"p1": 0}, ["p1"]) == [("p1", _t("a")), ("p1", _t("b"))]


def test_round_robin_interleave_two_projects():
    ready = {"p1": [_t("a1"), _t("a2"), _t("a3")], "p2": [_t("b1"), _t("b2")]}
    out = interleave_ready(ready, {"p1": 0, "p2": 0}, ["p1", "p2"])
    assert [(p, t["id"]) for p, t in out] == [
        ("p1", "a1"), ("p2", "b1"), ("p1", "a2"), ("p2", "b2"), ("p1", "a3"),
    ]


def test_least_loaded_project_goes_first():
    """REGRESSION PIN (2026-06-10): with budget 1/tick, arm-order round-robin
    starved every project but the first — least-loaded-first must put the
    project with fewer in-flight topics ahead, statelessly."""
    ready = {"p1": [_t("a1")], "p2": [_t("b1")]}
    out = interleave_ready(ready, {"p1": 2, "p2": 0}, ["p1", "p2"])
    assert [(p, t["id"]) for p, t in out] == [("p2", "b1"), ("p1", "a1")]


def test_tie_break_is_arm_order():
    ready = {"p2": [_t("b1")], "p1": [_t("a1")]}
    out = interleave_ready(ready, {"p1": 1, "p2": 1}, ["p1", "p2"])
    assert [p for p, _ in out] == ["p1", "p2"]


def test_fifty_vs_two_budget_five_prefix_is_fair():
    """Spec §2.7: first 5 interleaved entries contain BOTH small-project topics."""
    ready = {"big": [_t(f"x{i}") for i in range(50)], "small": [_t("s1"), _t("s2")]}
    out = interleave_ready(ready, {"big": 0, "small": 0}, ["big", "small"])
    first5 = [(p, t["id"]) for p, t in out[:5]]
    assert ("small", "s1") in first5 and ("small", "s2") in first5
    assert len(out) == 52


def test_missing_in_flight_defaults_zero_and_empty_projects_skipped():
    assert interleave_ready({"p1": [_t("a")]}, {}, ["p1", "ghost"]) == [("p1", _t("a"))]
    assert interleave_ready({"p1": []}, {"p1": 0}, ["p1"]) == []
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_scheduler.py -v
```

Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/juggle_graph_scheduler.py`**

```python
"""juggle_graph_scheduler — fair cross-project dispatch ordering (pure).

Owns: the least-loaded-first round-robin interleave deciding in which ORDER
ready TOPICS are claimed when several projects are armed (R3/R9 — the topic is
the budget unit: one topic = one thread = one agent; tasks are sequential
inside their topic and never scheduled here). The global capacity cap stays in
the dispatch path; because this ordering is fair, the prefix that fits under
the cap is fair too (a cap hit breaks the whole pass, spec DA A5).
Must not own: DB access, claiming, dispatching (juggle_graph_dispatch).

Policy (spec §2.7): sort armed projects by in-flight topic count ascending
(tie-break: arm order), then emit ready topics one per project per round.
Stateless + deterministic — no persisted cursor; self-balancing because last
tick's winners carry higher in-flight counts.
"""

from __future__ import annotations


def interleave_ready(
    ready_by_project: dict[str, list[dict]],
    in_flight: dict[str, int],
    armed_order: list[str],
) -> list[tuple[str, dict]]:
    """Fair cross-project dispatch order: list of (project_id, topic)."""
    rank = {pid: i for i, pid in enumerate(armed_order)}
    pids = [p for p in ready_by_project if ready_by_project[p]]
    pids.sort(key=lambda p: (in_flight.get(p, 0), rank.get(p, len(rank))))
    queues = {p: list(ready_by_project[p]) for p in pids}
    out: list[tuple[str, dict]] = []
    while queues:
        for pid in [p for p in pids if p in queues]:
            out.append((pid, queues[pid].pop(0)))
            if not queues[pid]:
                del queues[pid]
    return out
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest -q tests/test_graph_scheduler.py -v
git add src/juggle_graph_scheduler.py tests/test_graph_scheduler.py
git commit -m "feat: pure fair scheduler — least-loaded-first round-robin over topics (R3/R9)"
```

---

### Task 6: Tick — claim + dispatch TOPICS across armed projects

**Files:**
- Modify: `src/juggle_graph_dispatch.py` (`claim_topic`, `sweep_stale_topic_claims`, `graph_tick`, `_give_up_topic_dispatch`)
- Modify: `src/juggle_graph_hydration.py` (`hydrate_for_topic` + pure `build_topic_hydration`)
- Test: `tests/test_graph_dispatch.py` (adapt + append), `tests/test_graph_hydration_topics.py` (new)

- [ ] **Step 1: Write the failing hydration tests** (`tests/test_graph_hydration_topics.py`)

```python
"""Topic hydration (R9): objective + dep-TOPIC handoffs + SEQUENTIAL task list
+ the per-task mark-task contract. Never thread.summary (DA M4)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_hydration import build_topic_hydration  # noqa: E402


def _topic():
    return {"id": "auth", "title": "Authentication", "objective": "Login e2e."}


def _tasks():
    return [
        {"id": "t1", "title": "Schema", "prompt": "users table",
         "verify_cmd": "pytest tests -q", "state": "pending"},
        {"id": "t2", "title": "Endpoint", "prompt": "/login",
         "verify_cmd": None, "state": "verified"},
    ]


def test_topic_hydration_contains_contract_and_order():
    text = build_topic_hydration(
        "Proj objective", _topic(),
        deps=[{"id": "db", "title": "DB", "handoff": "schema v1", "diffstat": None}],
        tasks=_tasks(),
    )
    assert "Proj objective" in text and "Login e2e." in text
    assert "schema v1" in text                       # dep TOPIC handoff
    assert text.index("t1") < text.index("t2")       # sequential order preserved
    assert "mark-task" in text                       # per-task completion contract
    assert "complete-agent" in text                  # topic-level finish


def test_verified_task_flagged_for_skip():
    text = build_topic_hydration("", _topic(), deps=[], tasks=_tasks())
    assert "VERIFIED — skip" in text and "t2" in text
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_hydration_topics.py -v
```

Expected: FAIL — `ImportError: cannot import name 'build_topic_hydration'`.

- [ ] **Step 3: Implement hydration** (append to `src/juggle_graph_hydration.py`)

```python
def build_topic_hydration(objective: str, topic: dict, deps: list[dict],
                          tasks: list[dict]) -> str:
    """Dispatch prompt for a TOPIC (R9 hybrid): project objective + dep-topic
    handoffs (+ diffstat) + the topic objective + the SEQUENTIAL task list with
    the per-task TDD/commit/mark-task contract. Pure; never thread.summary."""
    parts = []
    if (objective or "").strip():
        parts.append(f"## Project objective\n{objective.strip()}")
    if deps:
        chunks = []
        for d in deps:
            handoff = (d.get("handoff") or "").strip() or "(no handoff recorded)"
            diffstat = (d.get("diffstat") or "").strip()
            if diffstat:
                handoff += f"\nIntegrated diffstat:\n{diffstat}"
            chunks.append(f"### {d['id']} — {d['title']}\n{handoff}")
        parts.append(
            "## Upstream topic handoffs (verified dependencies, already "
            "integrated into main)\n" + "\n".join(chunks)
        )
    parts.append(f"## Topic {topic['id']}: {topic['title']}\n"
                 f"{(topic.get('objective') or '').strip()}")
    rows = []
    for n in tasks:
        flag = " [VERIFIED — skip]" if n.get("state") == "verified" else ""
        vc = f"\nverify_cmd: {n['verify_cmd']}" if n.get("verify_cmd") else ""
        rows.append(f"### {n['id']} — {n['title']}{flag}{vc}\n{n['prompt']}")
    parts.append(
        "## Tasks — execute SEQUENTIALLY, in this order\n"
        "Per task: TDD (failing test first) → make it pass → run its "
        "verify_cmd → COMMIT → mark it:\n"
        "`juggle graph mark-task <task-id> --handoff '<files touched, "
        "interfaces changed, key decisions>'` (or `--fail` if you must give "
        "up on the task). Tasks flagged VERIFIED: skip them.\n\n"
        + "\n\n".join(rows)
    )
    parts.append(
        "## Finish\nWhen EVERY task above is marked, run "
        "`juggle complete-agent <thread> \"<summary>\"` — integrate runs ONCE "
        "for the whole topic. complete-agent REFUSES while tasks are unmarked."
    )
    return "\n\n".join(parts)


def hydrate_for_topic(db, project_id: str, topic: dict) -> str:
    """DB wrapper: dep-topic rows + topo-ordered tasks → build_topic_hydration."""
    from dbops import db_topics

    project = db.get_project(project_id) or {}
    deps = [db_topics.get_topic(db, t)
            for t in db_topics.derived_topic_deps(db, topic["id"])]
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    return build_topic_hydration(project.get("objective") or "", topic,
                                 [d for d in deps if d], tasks)
```

- [ ] **Step 4: Write the failing tick tests** (append to `tests/test_graph_dispatch.py`; reuse its `db` fixture and `FakeDispatch`)

```python
# ── multi-project TOPIC tick (R9, 2026-06-11) ─────────────────────────────────

from dbops import db_topics as tp  # noqa: E402


def _mk_topic(db, tid, project="INBOX", n_tasks=1, ready=True):
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    for i in range(n_tasks):
        nid = f"{tid}-k{i}"
        g.create_node(db, node_id=nid, project_id=project, title=nid, prompt="p")
        with db._connect() as conn:
            conn.execute("UPDATE graph_nodes SET topic_id=? WHERE id=?", (tid, nid))
            conn.commit()
    if ready:
        tp.recompute_topic_ready(db, project)


def _arm_many(db, *projects):
    db.set_setting(gd.ARMED_PROJECT_KEY, ",".join(projects))


def test_tick_dispatches_topics_across_all_armed_projects(db):
    """REGRESSION PIN (2026-06-10): the tick served get_armed_project() only —
    a second armed project never dispatched. Every armed graph must tick,
    and the dispatch unit is the TOPIC (R9): one dispatch per topic, not per
    task."""
    _mk_topic(db, "A1", "P1", n_tasks=3)
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    fd = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fd)
    assert sorted(stats["dispatched"]) == ["A1", "B1"]
    assert tp.get_topic(db, "A1")["state"] == "running"
    assert len(fd.calls) == 2, "one dispatch per TOPIC, not per task"


def test_each_thread_bound_to_its_topics_project(db):
    """REGRESSION PIN (2026-06-10 spec DA): cross-project thread mis-binding
    would hydrate the wrong objective. Thread.project_id must match its
    topic's project."""
    _mk_topic(db, "A1", "P1")
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    gd.graph_tick(db, dispatch_fn=FakeDispatch())
    for tid_, pid in (("A1", "P1"), ("B1", "P2")):
        th = tp.get_topic(db, tid_)["thread_id"]
        assert th and db.get_thread(th)["project_id"] == pid


def test_disarm_one_mid_batch_other_project_keeps_dispatching(db):
    """REGRESSION PIN (2026-06-10): old mid-batch guard stopped EVERYTHING on
    disarm — must skip only the disarmed project's topics."""
    for t_ in ("A1", "A2"):
        _mk_topic(db, t_, "P1")
    for t_ in ("B1", "B2"):
        _mk_topic(db, t_, "P2")
    _arm_many(db, "P1", "P2")

    class DisarmingDispatch(FakeDispatch):
        def __call__(self, db_, thread_id, prompt, topic):
            super().__call__(db_, thread_id, prompt, topic)
            if topic["id"].startswith("A"):
                db_.set_setting(gd.ARMED_PROJECT_KEY, "P2")

    stats = gd.graph_tick(db, dispatch_fn=DisarmingDispatch())
    dispatched = set(stats["dispatched"])
    assert {"B1", "B2"} <= dispatched
    assert len({"A1", "A2"} & dispatched) == 1


def test_poisoned_project_scan_does_not_block_others(db, monkeypatch):
    """REGRESSION PIN (R4): a ready-scan exception used to abort the whole
    tick; blast radius must be one project."""
    _mk_topic(db, "A1", "P1")
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    real = tp.recompute_topic_ready

    def boom(db_, pid):
        if pid == "P1":
            raise RuntimeError("poisoned graph")
        return real(db_, pid)

    monkeypatch.setattr(gd.db_topics, "recompute_topic_ready", boom)
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())
    assert stats["dispatched"] == ["B1"]


def test_global_cap_defers_fairly_across_projects(db):
    """Capacity is GLOBAL (MAX_THREADS bounds TOPICS — R9 budget model): a cap
    hit defers the pass with claims released; the fair prefix contains BOTH
    projects."""
    for i in range(3):
        _mk_topic(db, f"A{i}", "P1")
    _mk_topic(db, "B0", "P2")
    _arm_many(db, "P1", "P2")

    class CapAfter(FakeDispatch):
        def __init__(self, n):
            super().__init__()
            self.n = n
        def __call__(self, db_, thread_id, prompt, topic):
            if len(self.calls) >= self.n:
                raise gd.CapacityError("pool full")
            super().__call__(db_, thread_id, prompt, topic)

    stats = gd.graph_tick(db, dispatch_fn=CapAfter(2))
    assert len(stats["dispatched"]) == 2
    assert {t_[0] for t_ in stats["dispatched"]} == {"A", "B"}
    for tid_ in stats["deferred"]:
        assert tp.get_topic(db, tid_)["state"] == "ready", "claim released"


def test_single_project_single_topic_behavior_unchanged(db):
    """R6 pin: a 1-element armed set with synthetic 1-task topics behaves like
    the legacy flat tick (one dispatch, dep-gated)."""
    _mk_topic(db, "T-a")
    tp.create_topic(db, topic_id="T-b", project_id="INBOX", title="b")
    g.create_node(db, node_id="b", project_id="INBOX", title="b", prompt="p")
    with db._connect() as conn:
        conn.execute("UPDATE graph_nodes SET topic_id='T-b' WHERE id='b'")
        conn.execute(
            "INSERT INTO graph_edges (node_id, depends_on_id) VALUES ('b','T-a-k0')")
        conn.commit()
    tp.recompute_topic_ready(db, "INBOX")
    _arm(db)  # legacy scalar arm helper
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())
    assert stats["dispatched"] == ["T-a"]  # T-b gated on T-a via derived dep
```

The EXISTING node-level tests in this file: the `claim_node`/`sweep` tests stay
green untouched (those functions remain for tasks). Tests that drive
`graph_tick` over flat nodes must be ADAPTED to seed a synthetic topic per node
(via `_mk_topic`) — keep each test's asserted behavior identical and append
`(adapted to topics, R9 2026-06-11)` to the docstring. Never delete a pin.

- [ ] **Step 5: Implement in `src/juggle_graph_dispatch.py`**

Add `from dbops import db_topics` to the module imports, then the topic twins
(same SQL pattern, table swapped):

```python
def claim_topic(db, topic_id: str) -> bool:
    """Atomic ready→dispatching TOPIC claim (DA B4 pattern). True iff won."""
    with db._connect() as conn:
        cur = conn.execute(
            "UPDATE graph_topics SET state='dispatching', updated_at=? "
            "WHERE id=? AND state='ready'",
            (_now(), topic_id),
        )
        conn.commit()
        return cur.rowcount == 1


def sweep_stale_topic_claims(db, project_id: str) -> list[str]:
    """dispatching >10 min with no thread → ready (crash-safe, idempotent)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=STALE_CLAIM_SECS)
    ).isoformat()
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id FROM graph_topics WHERE project_id=? AND "
            "state='dispatching' AND thread_id IS NULL AND updated_at < ?",
            (project_id, cutoff),
        ).fetchall()
    stale = [r["id"] for r in rows]
    for tid in stale:
        db_topics.topic_transition(db, tid, "stale_reset")
        _log.warning("graph tick: stale TOPIC claim swept, %s → ready", tid)
    return stale


def _give_up_topic_dispatch(db, topic_id: str, err: Exception) -> None:
    """Retry cap reached: topic → failed-exec + derived-dependent blocking +
    ONE final action item (mirror of _give_up_dispatch)."""
    db_topics.mark_topic_exec_failed(db, topic_id)
    blocked = db_topics.propagate_topic_failure(db, topic_id)
    detail = f" Dependent topics blocked: {', '.join(blocked)}." if blocked else ""
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ Autopilot gave up on topic {topic_id} after "
            f"{MAX_DISPATCH_FAILS} consecutive dispatch failures: {err}.{detail} "
            f"Fix the dispatch path, then reload the graph spec to resume."
        ),
        type_="failure",
        priority="high",
    )
    _log.error("graph tick: topic %s failed-exec after %d dispatch failures",
               topic_id, MAX_DISPATCH_FAILS)
```

Replace `graph_tick` with:

```python
def graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict:
    """One dispatcher tick across ALL armed projects, claiming TOPICS (R9).

    Per project: topic stale-claim sweep + topic-ready recompute (a failure
    skips ONLY that project — R4). Ready topics are ordered fairly
    (juggle_graph_scheduler) and dispatched through the existing
    claim → thread → hydrate → dispatch body. ONE thread per topic: MAX_THREADS
    bounds concurrent topics; integrate runs once per topic at completion
    (bounds the integrate lock-storm class, commit 5fc261b). Never raises.
    """
    from juggle_graph_hydration import hydrate_for_topic
    from juggle_graph_scheduler import interleave_ready
    from juggle_graph_status import IN_FLIGHT_STATES

    stats: dict = {"dispatched": [], "swept": [], "deferred": [], "errors": []}
    armed = get_armed_projects(db)
    if not armed:
        return stats
    dispatch = dispatch_fn or _dispatch_via_pool

    ready_by_project: dict[str, list[dict]] = {}
    in_flight: dict[str, int] = {}
    for pid in armed:
        try:
            stats["swept"] += sweep_stale_topic_claims(db, pid)
            db_topics.recompute_topic_ready(db, pid)
            topics = db_topics.list_topics(db, pid)
        except Exception:
            _log.exception(
                "graph tick: ready-set scan failed for %s — skipping project", pid
            )
            continue
        ready_by_project[pid] = [t for t in topics if t["state"] == "ready"]
        in_flight[pid] = sum(1 for t in topics if t["state"] in IN_FLIGHT_STATES)

    for pid, topic in interleave_ready(ready_by_project, in_flight, armed):
        tid = topic["id"]
        if pid not in get_armed_projects(db):
            continue  # THIS project disarmed mid-batch — others keep going
        try:
            if not claim_topic(db, tid):
                continue  # another claimer won (DA B4)
            try:
                thread_id = db.create_thread(
                    f"[{tid}] {topic['title']}"[:80], session_id=_session_id(db)
                )
            except ValueError as e:
                db_topics.topic_transition(db, tid, "stale_reset")
                if "Maximum of" not in str(e):
                    stats["errors"].append(tid)
                    db.add_action_item(
                        thread_id=None,
                        message=(f"⚠️ Autopilot thread creation failed for "
                                 f"topic {tid}: {e}"),
                        type_="failure", priority="high",
                    )
                    continue
                stats["deferred"].append(tid)
                _log.info("graph tick: thread cap hit — topic %s deferred", tid)
                break  # cap is global; later topics would hit it too
            db.update_thread(thread_id, project_id=pid)
            # Bind BEFORE send-task (DA round-2 MAJOR-4): a crash in the
            # dispatch window must leave the topic thread-bound so the stale
            # sweep cannot reclaim and double-dispatch it.
            db_topics.set_topic_thread(db, tid, thread_id)
            fail_key = (str(db.db_path), tid)
            try:
                dispatch(db, thread_id, hydrate_for_topic(db, pid, topic), topic)
            except CapacityError:
                db.archive_thread(thread_id)
                db_topics.set_topic_thread(db, tid, None)
                db_topics.topic_transition(db, tid, "stale_reset")
                stats["deferred"].append(tid)
                break
            except Exception as e:
                db.archive_thread(thread_id)
                db_topics.set_topic_thread(db, tid, None)
                stats["errors"].append(tid)
                fails = _dispatch_fails.get(fail_key, 0) + 1
                _dispatch_fails[fail_key] = fails
                if fails >= MAX_DISPATCH_FAILS:
                    _dispatch_fails.pop(fail_key, None)
                    _give_up_topic_dispatch(db, tid, e)
                else:
                    db_topics.topic_transition(db, tid, "stale_reset")
                    db.add_action_item(
                        thread_id=None,
                        message=(f"⚠️ Autopilot dispatch failed for topic {tid} "
                                 f"(attempt {fails}/{MAX_DISPATCH_FAILS}): {e}"),
                        type_="failure", priority="high",
                    )
                continue
            _dispatch_fails.pop(fail_key, None)
            db_topics.topic_transition(db, tid, "dispatch")  # → running
            db.add_notification_v2(
                thread_id=thread_id,
                message=f"⬢ autopilot dispatched topic {tid} — {topic['title']}",
                session_id=_session_id(db),
            )
            stats["dispatched"].append(tid)
        except Exception:
            _log.exception("graph tick: unexpected error on topic %s", tid)
            stats["errors"].append(tid)
    return stats
```

Keep the NODE-level `claim_node`/`sweep_stale_claims`/`_give_up_dispatch` in
place (tasks still use the machine; tests pin them). Check the LOC gate
(`wc -l src/juggle_graph_dispatch.py`); if meaningfully over ~310, extract the
topic claim/sweep/give-up trio to `src/juggle_graph_dispatch_topics.py` as a
separate mechanical-refactor commit (tests green before and after).

- [ ] **Step 6: Run suites + commit**

```bash
uv run pytest -q tests/test_graph_dispatch.py tests/test_graph_hydration_topics.py tests/test_llm_dispatch.py -v 2>&1 | tail -5
git add src/juggle_graph_dispatch.py src/juggle_graph_hydration.py tests/test_graph_dispatch.py tests/test_graph_hydration_topics.py
git commit -m "feat: tick claims + dispatches TOPICS fairly across armed projects (R2/R3/R4/R9)"
```

---

### Task 7: Completion — `graph mark-task`, complete-agent topic gate, integrate-once

**Files:**
- Modify: `src/juggle_cmd_graph.py` (`mark-task` subcommand)
- Modify: `src/juggle_cmd_agents_graph.py` (`check_topic_completion_gate`, `mark_graph_topic`)
- Modify: the `cmd_complete_agent` seam (find it: `grep -rn "mark_graph_node\|_run_integrate" src/ --include="*.py" | grep -v test`)
- Test: `tests/test_graph_marking.py` (append), `tests/test_graph_contract.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_contract.py`; READ its `_bind_running_thread`/`_complete` helpers and integrate monkeypatch first, then write these as REAL tests following that machinery, binding `graph_topics.thread_id` instead of the node)

```python
# ── topic completion gate + marking (R9, 2026-06-11) ──────────────────────────


def test_complete_agent_refuses_while_tasks_unmarked(db):
    """REGRESSION PIN (2026-06-11 R9/A10): complete-agent on a topic thread
    with non-terminal tasks must REFUSE (exit 1) BEFORE integrate — nothing
    marked, nothing merged. The gate is code, not prompt."""
    # seed topic 'A' running + thread bound; tasks a1 verified, a2 pending.
    # act: drive the completion path; assert SystemExit; topic still
    # 'running'; a2 still 'pending'; the integrate stub was NOT called.


def test_complete_agent_marks_topic_when_all_tasks_terminal(db):
    # all tasks verified → topic 'verified', handoff stored; integrate stub
    # called exactly ONCE (integrate-once-per-topic, spec §2.3).


def test_topic_with_failed_task_completes_as_failed_verify(db):
    # a1 verified, a2 failed-verify (terminal) → gate passes, verify_ok=False
    # → topic 'failed-verify'; derived dependent topics → blocked-failed.
```

And the `mark-task` CLI (append to `tests/test_graph_marking.py`, following its
fixture style):

```python
def test_mark_task_verifies_and_stores_handoff(db, capsys):
    """`juggle graph mark-task t1 --handoff '…'` walks the task to 'verified'
    via the EXISTING node machine. Task 'verified' = committed in topic
    worktree + verify_cmd green — NOT merged (merged is TOPIC-level,
    spec §2.3)."""
    # create topic+task t1; call cmd_graph_mark_task(Namespace(task_id="t1",
    # fail=False, handoff="did things", db_path=...)); assert task state
    # 'verified' and handoff stored.


def test_mark_task_fail_maps_to_failed_verify(db, capsys):
    # Namespace(fail=True) → task state 'failed-verify'.
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_contract.py tests/test_graph_marking.py -k "topic or mark_task" -v
```

Expected: FAIL — no handler, no gate.

- [ ] **Step 3: Implement `mark-task`** (in `src/juggle_cmd_graph.py`; register beside `add-node`)

```python
def cmd_graph_mark_task(args):
    """`juggle graph mark-task <task-id> [--fail] [--handoff '…']` — the topic
    agent's per-task completion (R9 hybrid). Maps onto the EXISTING node
    machine via mark_completion(integrate_ok=True, verify_ok=not --fail):
    task 'verified' = committed-in-topic-worktree + verify_cmd green —
    verified-means-MERGED holds at TOPIC level only (spec §2.3)."""
    db = get_db(getattr(args, "db_path", None), init=True)
    task = db_graph.get_node(db, args.task_id)
    if not task:
        print(f"Error: task {args.task_id!r} not found.", file=sys.stderr)
        sys.exit(1)
    try:
        state = db_graph.mark_completion(
            db, args.task_id, integrate_ok=True,
            verify_ok=not getattr(args, "fail", False),
            handoff=getattr(args, "handoff", None),
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"task {args.task_id} → {state}")
```

Parser: positional `task_id`, `--fail` (store_true), `--handoff` (default None).

- [ ] **Step 4: Implement the topic completion path** (in `src/juggle_cmd_agents_graph.py`)

```python
def check_topic_completion_gate(db, thread_uuid) -> str | None:
    """R9/A10 gate: complete-agent on a topic thread refuses while any task is
    non-terminal. Returns refusal message or None. MUST run BEFORE integrate."""
    from dbops import db_topics

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return None  # pre-migration DB
    if not topic:
        return None  # not a topic thread — normal completion
    terminal = {"verified", "failed-exec", "failed-integration",
                "failed-verify", "blocked-failed"}
    open_tasks = [n["id"] for n in db_topics.list_topic_tasks(db, topic["id"])
                  if n["state"] not in terminal]
    if open_tasks:
        return (
            f"topic {topic['id']} has unmarked task(s): {', '.join(open_tasks)} "
            f"— mark each with `juggle graph mark-task <id> --handoff '…'` "
            f"(or --fail) before complete-agent. Nothing was marked or merged."
        )
    return None


def mark_graph_topic(db, thread_uuid, integrate_ok, handoff, session_id,
                     *, verify_failed=False):
    """Topic twin of mark_graph_node: map (integrate, verify) outcomes onto the
    TOPIC machine; verify_ok additionally requires every task 'verified'.
    Falls back to mark_graph_node for legacy node-bound threads."""
    from dbops import db_topics

    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        return  # pre-migration DB without graph tables
    if not topic:
        return mark_graph_node(db, thread_uuid, integrate_ok, handoff,
                               session_id, verify_failed=verify_failed)
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    all_verified = bool(tasks) and all(n["state"] == "verified" for n in tasks)
    try:
        state = db_topics.mark_topic_completion(
            db, topic["id"],
            integrate_ok=integrate_ok or verify_failed,
            verify_ok=(not verify_failed) and all_verified,
            handoff=handoff,
        )
    except ValueError as e:
        print(f"Warning: graph topic {topic['id']} not marked — {e}")
        return
    if state == "verified":
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} verified (merged)",
            session_id=session_id,
        )
    else:
        blocked = db_topics.propagate_topic_failure(db, topic["id"])
        detail = (f" Dependent topics blocked: {', '.join(blocked)}."
                  if blocked else "")
        db.add_notification_v2(
            thread_id=thread_uuid,
            message=f"⬢ topic {topic['id']} → {state}",
            session_id=session_id,
        )
        db.add_action_item(
            thread_id=None,
            message=(f"Topic {topic['id']} failed ({state}).{detail} Fix and "
                     f"reload the graph spec to resume."),
            type_="failure", priority="high",
        )
    for ready_id in db_topics.recompute_topic_ready(db, topic["project_id"]):
        rt = db_topics.get_topic(db, ready_id)
        title = rt["title"] if rt else ready_id
        db.add_notification_v2(
            thread_id=None,
            message=f"⬢ topic ready: {ready_id} — {title}",
            session_id=session_id,
        )
```

Wire-up: locate the completion entrypoint (`grep -rn "mark_graph_node("
src/ --include="*.py" | grep -v test` — likely `juggle_cmd_agents_tasks.py` or
a sibling): (a) BEFORE the integrate step, call `check_topic_completion_gate`;
on refusal print the message and `sys.exit(1)`; (b) replace the
`mark_graph_node(...)` call with `mark_graph_topic(...)` (same arguments — it
falls back for legacy threads). The integrate machinery itself is UNTOUCHED:
it is already per-thread, and the topic owns the thread — integrate-once-per-
topic is structural (the lock-storm fix, commit 5fc261b).

Agent-death wiring: find the existing node-failure path
(`grep -n "mark_exec_failed\|_mark_thread_failed" src/*.py`) and add the topic
equivalent: topic-by-thread → `db_topics.mark_topic_exec_failed` +
`propagate_topic_failure` (per-task states are NOT touched — that is the
resume story, spec DA A9). Adapt one test in `tests/test_graph_agent_death.py`
to a topic thread asserting per-task states are preserved; docstring
`(adapted to topics, R9 2026-06-11)`.

- [ ] **Step 5: Run suites + commit**

```bash
uv run pytest -q tests/test_graph_contract.py tests/test_graph_marking.py tests/test_graph_agent_death.py tests/test_cmd_graph.py -v 2>&1 | tail -5
git add src/juggle_cmd_graph.py src/juggle_cmd_agents_graph.py src/juggle_cmd_agents_tasks.py tests/
git commit -m "feat: mark-task + topic completion gate + integrate-once-per-topic (R9)"
```

---

### Task 8: CLI (arm/disarm/off/status), hooks (R7), R8 guard

**Files:**
- Modify: `src/juggle_cmd_autopilot.py`, `src/juggle_hooks_autopilot.py`, `src/juggle_cmd_agents_graph.py` (guard), `src/juggle_graph_status.py` (topic-aware injection)
- Test: `tests/test_cmd_autopilot.py` (append), `tests/test_hooks_autopilot_multi.py` (new), `tests/test_graph_contract.py` (append)

- [ ] **Step 1: CLI — failing tests** (append to `tests/test_cmd_autopilot.py`, reusing `db_path`/`db`/`flag` fixtures and `_args`)

```python
# ── multi-project arming + topic status (2026-06-11) ─────────────────────────


def test_arm_second_project_adds_not_replaces(db_path, db, flag, capsys):
    """REGRESSION PIN (2026-06-10): arming a second project silently REPLACED
    the first (scalar overwrite) — arm must ADD."""
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    assert db.get_setting(ARMED_PROJECT_KEY) == "INBOX,p2"


def test_disarm_one_keeps_rest_flag_untouched(db_path, db, flag, capsys):
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    ap.cmd_autopilot(_args(db_path, "disarm", "INBOX"))
    assert db.get_setting(ARMED_PROJECT_KEY) == "p2"
    assert flag.exists()


def test_disarm_unknown_project_fails_loud(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    with pytest.raises(SystemExit):
        ap.cmd_autopilot(_args(db_path, "disarm", "nope"))
    assert db.get_setting(ARMED_PROJECT_KEY) == "INBOX"


def test_off_one_project_clears_flag_only_when_set_empties(db_path, db, flag, capsys):
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    ap.cmd_autopilot(_args(db_path, "off", "INBOX"))
    assert flag.exists(), "flag clears only when the set empties"
    ap.cmd_autopilot(_args(db_path, "off", "p2"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None and not flag.exists()


def test_status_lists_topic_and_task_counts_per_project(db_path, db, flag, capsys):
    import json as _json
    from dbops import db_topics as tp
    db.create_project("p2", name="P2")
    tp.create_topic(db, topic_id="T1", project_id="INBOX", title="T1")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    payload = _json.loads(capsys.readouterr().out)
    assert payload["armed_projects"] == ["INBOX", "p2"]
    assert payload["graphs"]["INBOX"]["topics"]["total"] == 1
    assert payload["graphs"]["p2"] is None
    assert payload["armed_project"] == "INBOX"  # deprecated, one release
```

(If `db.create_project` differs from the real projects API, grep
`src/dbops/projects.py` and adapt mechanics, not intent.)

- [ ] **Step 2: Implement the CLI** (`src/juggle_cmd_autopilot.py`)

Import the Task 1 accessors. In `_cmd_arm`, replace
`db.set_setting(ARMED_PROJECT_KEY, project_id)` with:

```python
    try:
        armed = arm_project(db, project_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

and print the set when >1 armed (`Armed set: a, b.` suffix). Add the shared
remover and rewire the subcommands:

```python
def _cmd_remove(db, project_id: str | None, *, clear_flag_when_empty: bool) -> None:
    """disarm/off: remove one project (or all), set-aware flag handling."""
    if project_id:
        if project_id not in get_armed_projects(db):
            print(f"Error: project {project_id!r} is not armed.", file=sys.stderr)
            sys.exit(1)
        remaining = disarm_project(db, project_id)
    else:
        set_armed_projects(db, [])
        remaining = []
    if clear_flag_when_empty and not remaining:
        _flag_set(False)
    rest = f" Still armed: {', '.join(remaining)}." if remaining else ""
    what = f"Project {project_id} disarmed." if project_id else "All projects disarmed."
    print(f"{what}{rest} Global autopilot: "
          f"{'ON' if AUTOPILOT_FLAG.exists() else 'OFF'}.")
```

`cmd_autopilot`: `disarm` → `_cmd_remove(..., clear_flag_when_empty=False)`;
`off` → `_cmd_remove(..., clear_flag_when_empty=True)`. In `register()`,
give `disarm`/`off` an optional positional
(`add_argument("project", nargs="?", default=None)`); `on`/`status` keep
`project=None` defaults. Rewrite `_cmd_status`:

```python
def _cmd_status(db, json_out: bool) -> None:
    from dbops.db_topics import topic_counts
    from juggle_graph_status import format_progress, graph_counts

    global_on = AUTOPILOT_FLAG.exists()
    armed = get_armed_projects(db)
    graphs = {}
    for pid in armed:
        tc, nc = topic_counts(db, pid), graph_counts(db, pid)
        graphs[pid] = {"topics": tc, "tasks": nc} if (tc or nc) else None
    diverged = bool(armed) and not global_on
    if json_out:
        first = armed[0] if armed else None
        print(json.dumps({
            "global_on": global_on, "armed_projects": armed, "graphs": graphs,
            "diverged": diverged,
            "armed_project": first,                         # deprecated (1 release)
            "graph": graphs.get(first) if first else None,  # deprecated
        }))
        return
    print(f"Autopilot global: {'ON' if global_on else 'OFF'}")
    if not armed:
        print("Armed projects: (none)")
    else:
        print(f"Armed projects ({len(armed)}): {', '.join(armed)}")
        for pid in armed:
            info = graphs[pid]
            if not info:
                print(f"  {pid}: no graph loaded")
                continue
            seg = []
            if info["topics"]:
                seg.append("topics " + format_progress(info["topics"]))
            if info["tasks"]:
                seg.append("tasks " + format_progress(info["tasks"]))
            print(f"  {pid}: " + "; ".join(seg))
    if diverged:
        print(
            "WARNING: settings key and flag file diverge — project(s) "
            f"{', '.join(armed)} armed but the global flag ({AUTOPILOT_FLAG}) "
            "is OFF: hooks inject nothing while the tick still dispatches."
        )
```

- [ ] **Step 3: Hooks — failing tests + implementation** (`tests/test_hooks_autopilot_multi.py`)

```python
"""Hooks inject the FULL armed set with TOPIC-level status (R7/R9)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_hooks_autopilot as ha  # noqa: E402
import juggle_hooks_config as _cfg  # noqa: E402
from juggle_autopilot_state import ARMED_PROJECT_KEY  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "hooks.db"))
    d.init_db()
    monkeypatch.setattr(_cfg, "get_db", lambda: d)
    return d


def test_carveout_names_every_armed_project_and_addnode_route(db):
    """REGRESSION PIN (2026-06-10): the carve-out named ONE project — an agent
    could treat project 2's topics as manually dispatchable."""
    tp.create_topic(db, topic_id="A1", project_id="P1", title="a")
    tp.create_topic(db, topic_id="B1", project_id="P2", title="b")
    db.set_setting(ARMED_PROJECT_KEY, "P1,P2")
    ctx = ha._armed_graph_context()
    assert "P1, P2" in ctx.splitlines()[0] and "add-node" in ctx
    assert "Graph [P1]" in ctx and "Graph [P2]" in ctx


def test_injection_budget_split_keeps_total_bounded(db):
    for pid in ("P1", "P2", "P3"):
        for i in range(8):
            tp.create_topic(db, topic_id=f"{pid}-t{i}", project_id=pid, title=f"t{i}")
    db.set_setting(ARMED_PROJECT_KEY, "P1,P2,P3")
    ctx = ha._armed_graph_context()
    lines = [l for l in ctx.splitlines() if l.startswith("Graph [")]
    assert len(lines) == 3 and sum(len(l) for l in lines) <= 540


def test_disarmed_returns_empty(db):
    assert ha._armed_graph_context() == ""
```

Implementation in `src/juggle_hooks_autopilot.py`:

```python
_ARMED_CARVEOUT = (
    "ARMED PROJECTS {projects}: topics of any armed project are tick-owned — "
    "NEVER dispatch them manually; report status only. NEW work for an armed "
    "project goes in as a task: `juggle graph add-node … --topic <t>` "
    "(code-enforced — manual send-task is refused without --force-node). "
    "The watchdog tick claims, dispatches, and completes topics; integrate "
    "runs once per topic."
)


def _armed_graph_context() -> str:
    """Carve-out + budgeted topic status for EVERY armed project, else ''.

    Authority is the armed-set settings key (DA M6). The per-project injection
    budget is the 500-char discipline split across the set (floor 160) so
    total stays bounded for any N. Degrades to '' on any DB error."""
    try:
        from juggle_autopilot_state import get_armed_projects
        from juggle_graph_status import INJECTION_BUDGET, build_graph_injection

        db = _cfg.get_db()
        armed = get_armed_projects(db)
        if not armed:
            return ""
        per = max(160, INJECTION_BUDGET // len(armed))
        lines = [_ARMED_CARVEOUT.format(projects=", ".join(armed))]
        lines += [build_graph_injection(db, pid, budget=per) for pid in armed]
        return "\n".join(lines)
    except Exception as exc:
        logging.warning("armed-project graph context failed: %s", exc)
        return ""
```

And make `build_graph_injection` (in `src/juggle_graph_status.py`) topic-aware:
when `graph_topics` has rows for the project, the line is
`Graph [pid]: topics <progress>; tasks <progress>. ready: <topic ids+titles>.
running: <topic ids (tasks k/n)>.` — same deterministic hard truncation at
`budget`; topics absent (pre-migration) → existing node-based line.

- [ ] **Step 4: R8 guard — failing tests + implementation** (append to `tests/test_graph_contract.py`)

```python
# ── armed-project dispatch guard (R8, adapted to topics 2026-06-11) ───────────


def test_guard_refuses_unbound_thread_of_armed_project(db):
    """REGRESSION PIN (2026-06-10 R8): ad-hoc send-task to an armed project's
    threads bypassed the graph — new work must route through add-node."""
    from juggle_cmd_agents_graph import check_node_guard
    from juggle_autopilot_state import arm_project

    arm_project(db, "INBOX")
    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, project_id="INBOX")
    err = check_node_guard(db, tid, force=False)
    assert err and "add-node" in err and "force-node" in err
    assert check_node_guard(db, tid, force=True) is None


def test_guard_lifts_on_disarm_and_ignores_unarmed(db):
    from juggle_cmd_agents_graph import check_node_guard
    from juggle_autopilot_state import arm_project, disarm_project

    arm_project(db, "OTHER")
    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, project_id="INBOX")
    assert check_node_guard(db, tid, force=False) is None  # unarmed project
    arm_project(db, "INBOX")
    assert check_node_guard(db, tid, force=False) is not None
    disarm_project(db, "INBOX")
    assert check_node_guard(db, tid, force=False) is None


def test_guard_topic_bound_operator_state_allowed(db):
    """R8 must not tighten DA B5: a TOPIC-bound thread in operator territory
    (failed-exec) stays manually redispatchable even while armed."""
    from dbops import db_topics as tp_
    from juggle_cmd_agents_graph import check_node_guard
    from juggle_autopilot_state import arm_project

    arm_project(db, "INBOX")
    tp_.create_topic(db, topic_id="T1", project_id="INBOX", title="t")
    tid = db.create_thread("t", session_id="s")
    tp_.set_topic_thread(db, "T1", tid)
    with db._connect() as conn:
        conn.execute("UPDATE graph_topics SET state='failed-exec' WHERE id='T1'")
        conn.commit()
    assert check_node_guard(db, tid, force=False) is None
```

Implementation — replace `check_node_guard` in `src/juggle_cmd_agents_graph.py`:

```python
def check_node_guard(db, thread_uuid, *, force: bool) -> str | None:
    """DA B5 + R8 (3-tier): manual dispatch that fights the tick is refused.

    TOPIC-bound thread in a tick-owned state → double-dispatch race (DA B5,
    lifted node→topic; legacy node bindings still checked). Unbound thread of
    an ARMED project → new work must enter the graph as a task (R8, spec
    §2.11). None = dispatch may proceed.
    """
    from dbops import db_graph, db_topics

    if force or not thread_uuid:
        return None
    try:
        topic = db_topics.get_topic_by_thread(db, thread_uuid)
    except Exception:
        topic = None  # pre-migration DB
    bound = topic or _node_for_thread(db, thread_uuid)  # legacy node fallback
    if bound:
        if bound["state"] not in db_graph.TICK_OWNED_STATES:
            return None  # operator territory — DA B5 unchanged
        kind = "topic" if topic else "node"
        return (
            f"thread is bound to graph {kind} {bound['id']} in tick-owned "
            f"state {bound['state']!r} — the autopilot watchdog tick "
            f"dispatches it. Use --force-node to override."
        )
    from juggle_autopilot_state import get_armed_projects

    thread = db.get_thread(thread_uuid) or {}
    pid = thread.get("project_id")
    if pid and pid in get_armed_projects(db):
        return (
            f"thread belongs to ARMED project {pid} — new work must enter its "
            f"graph: `juggle graph add-node … --topic <t> --project {pid}` "
            "(the tick dispatches it). Narrow exceptions (graph-machinery "
            "fixes; planning whose output IS the nodes): re-run with "
            "--force-node."
        )
    return None
```

- [ ] **Step 5: Run + commit**

```bash
uv run pytest -q tests/test_cmd_autopilot.py tests/test_hooks_autopilot_multi.py tests/test_graph_contract.py tests/test_graph_status.py tests/test_cli_agents.py -v 2>&1 | tail -5
git add src/juggle_cmd_autopilot.py src/juggle_hooks_autopilot.py src/juggle_cmd_agents_graph.py src/juggle_graph_status.py tests/
git commit -m "feat: set-aware autopilot CLI, topic-aware hooks, R8 armed-project guard"
```

If a pre-existing test asserts the old singular carve-out / status wording,
update the assertion to the new wording (behavior contract unchanged).

---

### Task 9: Cockpit — project → topic → task tree (R5)

**Files:**
- Modify: `src/juggle_cockpit_graph_dag.py` (CSV armed set; TOPICS as DAG nodes; task lists for the modal)
- Modify: `src/juggle_cockpit_graph_layout.py` (`GraphNode` gains optional `tasks_done`/`tasks_total`, default None)
- Modify: `src/juggle_cockpit_graph_panel.py` (task-progress cell suffix; multi-DAG stacking)
- Modify: `src/juggle_cockpit_model.py` (`graph_dags` list on `CockpitState` + first-element `graph_dag` shim)
- Modify: `src/juggle_cockpit_graph_mode.py` + `src/juggle_cockpit_modals.py` (render via list; modal lists the topic's tasks)
- Test: `tests/test_cockpit_graph_dag_load.py`, `tests/test_cockpit_graph_panel.py` (append)

- [ ] **Step 1: Loader — failing tests** (append to `tests/test_cockpit_graph_dag_load.py`; READ the file and reuse its conn/seed helpers — write REAL tests for these contracts)

```python
def test_load_graph_dags_topics_are_the_dag_nodes(...):
    """REGRESSION PIN (2026-06-11 R5/R9): the loader rendered TASKS as DAG
    nodes and read the armed key as a scalar. Nodes must be TOPICS with task
    progress; edges the DERIVED topic deps; one GraphDag per armed project
    (CSV), arm order."""
    # seed: P1 topics A (2 tasks, 1 verified) and B with a task edge B→A;
    #       P2 topic C with 1 task; settings key 'P1,P2'.
    # assert: [d.project_id for d in dags] == ["P1", "P2"];
    #         {n.id for n in dags[0].nodes} == {"A", "B"};
    #         dags[0].edges == [("B", "A")];
    #         the A node has tasks_done == 1 and tasks_total == 2;
    #         dags[0].tasks["A"] lists both task ids (modal tier).


def test_load_graph_dag_shim_returns_first(...):
    # settings 'P1,P2' → load_graph_dag(conn).project_id == "P1"


def test_load_graph_dags_empty_when_disarmed(...):
    # no settings row → load_graph_dags(conn) == []
```

- [ ] **Step 2: Implement the loader** (`src/juggle_cockpit_graph_dag.py`)

`_armed_set(conn)`: CSV parse of the settings value (strip, dedupe, order —
mirror of the accessor; the cockpit reads raw SQL by design). `_load_one(conn,
pid)`: select topics (`SELECT id, title, state, thread_id FROM graph_topics
WHERE project_id=? ORDER BY created_at, id`); per-topic task counts
(`SELECT topic_id, SUM(state='verified') AS done, COUNT(*) AS total FROM
graph_nodes WHERE topic_id IS NOT NULL GROUP BY topic_id`); derived edges
(same join as `db_topics.derived_topic_deps`, restricted to this project's
topics); task lists (`id,title,state` per topic) attached as `GraphDag.tasks:
dict[str, list]` (add the field to the dataclass, default `None`). Build
`GraphNode(id, title, state, thread_id, tasks_done, tasks_total)`. Return None
when the project has no topics. `load_graph_dags(conn)` = list comp over
`_armed_set`; `load_graph_dag(conn)` = first-or-None shim. In
`juggle_cockpit_graph_layout.py`, add to the frozen `GraphNode` dataclass:
`tasks_done: "int | None" = None` and `tasks_total: "int | None" = None`
(additive, default None — existing constructors stay valid).

- [ ] **Step 3: Panel — failing tests then implement** (append to `tests/test_cockpit_graph_panel.py`, using its render-to-text helper)

```python
def test_topic_cell_shows_task_progress(...):
    # GraphNode(id="auth", state="running", tasks_done=2, tasks_total=6)
    # → rendered panel text contains "auth 2/6"


def test_multi_panel_stacks_each_armed_dag_with_header(...):
    """REGRESSION PIN (2026-06-11): graph panel rendered only the first armed
    DAG — with two dags both project headers must render, P1 before P2."""
    # build two small dag inputs; build_multi_graph_panel; assert both
    # "P1 ·" and "P2 ·" appear in order.
```

Implement in `src/juggle_cockpit_graph_panel.py`: in `_cell_text`, append
`f" {node.tasks_done}/{node.tasks_total}"` to the label when
`getattr(node, "tasks_total", None)` (before the truncation logic). Extract the
post-title body of `build_graph_panel` (header+grid+minimap, currently lines
~106–137) into `_graph_section(project_id, nodes, edges, sel_id, inner_w,
pan_offset) -> list`; `build_graph_panel` becomes a thin wrapper
(pure-mechanical — existing panel tests stay green unmodified). Add:

```python
def build_multi_graph_panel(*, dags, selection, unread, width, height,
                            pan_offset) -> Panel:
    """Stacked multi-DAG panel: one titled topic-DAG section per armed project.
    selection indexes the concatenated flat selectable list across dags."""
    title = f"Graph{_badge_segment(unread)}"
    if not dags:
        body = Text("no armed graph — arm a project with /juggle:toggle-autopilot",
                    style=Style(dim=True))
        return Panel(body, title=title, border_style="grey50")
    if len(dags) == 1:
        d = dags[0]
        return build_graph_panel(
            project_id=d.project_id, nodes=d.nodes, edges=d.edges,
            selection=selection, unread=unread, width=width, height=height,
            pan_offset=pan_offset,
        )
    inner_w = max(8, width - 4)
    flat = [n for d in dags for n in _flat_selectable(d.nodes)]
    sel_id = flat[selection].id if 0 <= selection < len(flat) else None
    parts: list = []
    for i, d in enumerate(dags):
        if i:
            parts.append(Text("─" * inner_w, style=Style(dim=True)))
        parts.extend(_graph_section(d.project_id, d.nodes, d.edges, sel_id,
                                    inner_w, pan_offset))
    return Panel(_Group(*parts), title=title, border_style="cyan")
```

- [ ] **Step 4: Model + mode + modal** — `CockpitState` gains
`graph_dags: "list | None" = None`; where `snapshot(load_graph_dag=True)` sets
`graph_dag`, set `graph_dags = _load_graph_dags(conn)` and
`graph_dag = graph_dags[0] if graph_dags else None` (shim, one release).
`_render_graph_panel` in `juggle_cockpit_graph_mode.py`: build
`dags = getattr(state, "graph_dags", None) or ([state.graph_dag] if
getattr(state, "graph_dag", None) else [])`, clamp `self._graph_sel` to the
concatenated topic count, call `build_multi_graph_panel`. Check the rest of the
mixin (`_graph_select`, modal launch) for `state.graph_dag` uses and route
through the same concatenated list. In the detail modal
(`_GraphNodeModal`, `src/juggle_cockpit_modals.py` — read it first): when the
selected topic's dag carries `tasks`, append a task-list section
(state glyph + id + title per task, `NODE_STATE_GLYPHS`) — this is the TASK
tier of the R5 tree.

- [ ] **Step 5: Run cockpit suites + viewport matrix**

```bash
uv run pytest -q tests/test_cockpit_graph_dag_load.py tests/test_cockpit_graph_panel.py tests/test_cockpit_graph.py tests/test_cockpit_graph_keys.py tests/test_cockpit_graph_layout.py 2>&1 | tail -3
uv run src/juggle_cli.py cockpit --smoke --all-viewports
uv run src/juggle_cli.py cockpit --out | head -40
```

Expected: tests ALL PASS; all 7 viewport profiles green; `--out` renders clean.

- [ ] **Step 6: Commit**

```bash
git add src/juggle_cockpit_graph_dag.py src/juggle_cockpit_graph_layout.py src/juggle_cockpit_graph_panel.py src/juggle_cockpit_model.py src/juggle_cockpit_graph_mode.py src/juggle_cockpit_modals.py tests/
git commit -m "feat: cockpit project→topic→task tree — topic DAGs stacked per armed project (R5/R9)"
```

---

### Task 10: Full gates, e2e scenario, version bump

**Files:** `.claude-plugin/plugin.json`, `TODO.md`

- [ ] **Step 1: Full harness smoke gate (CLAUDE.md mandatory)**

```bash
export _JUGGLE_TEST_DB="$HOME/.claude/juggle/juggle.db"
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"
export JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q 2>&1 | tail -3
uv run src/juggle_cli.py doctor --dry-run
uv run src/juggle_cli.py cockpit --smoke --all-viewports
```

Expected: pytest green except failures PROVEN pre-existing on base (re-run
those exact ids on base; list in the completion summary). **Paste the pytest
summary line into the completion result — claims without harness evidence are
invalid.**

- [ ] **Step 2: End-to-end migration + arm/status scenario (deterministic)**

```bash
T=$(mktemp -d); export _JUGGLE_TEST_DB="$T/e2e.db"
uv run src/juggle_cli.py init-db
uv run python - <<'EOF'
import os, sys
sys.path.insert(0, "src")
from juggle_db import JuggleDB
from dbops import db_graph as g, db_topics as tp
db = JuggleDB(db_path=os.environ["_JUGGLE_TEST_DB"])
g.create_node(db, node_id="legacy", project_id="INBOX", title="L", prompt="p")
with db._connect() as c:
    c.execute("UPDATE graph_nodes SET topic_id=NULL WHERE id='legacy'"); c.commit()
from dbops.migrations_recent import run_recent_migrations
with db._connect() as c:
    run_recent_migrations(c); c.commit()
assert g.get_node(db, "legacy")["topic_id"] == "T-legacy"
assert tp.get_topic(db, "T-legacy")["state"] == "pending"
print("migration e2e OK")
EOF
uv run src/juggle_cli.py autopilot arm INBOX
uv run src/juggle_cli.py autopilot status --json
uv run src/juggle_cli.py autopilot off INBOX
uv run src/juggle_cli.py autopilot status --json
```

Expected: `migration e2e OK`; first JSON `"armed_projects": ["INBOX"]` with
`graphs.INBOX.topics.total == 1`; second `"armed_projects": []`,
`"global_on": false`.

- [ ] **Step 3: A2 grep gate (spec DA) + version + TODO + graphify**

```bash
grep -rn "autopilot_armed_project" src/ | grep -v "juggle_autopilot_state.py\|juggle_cockpit_graph_dag.py"
```

Expected: only re-export imports and docstrings — any remaining raw READER of
the key value is a bug; fix before completing. Then bump
`.claude-plugin/plugin.json` minor (feature → next `1.x.0`), mark the item done
in `TODO.md`, run `graphify update . || true`, and commit:

```bash
git add .claude-plugin/plugin.json TODO.md graphify-out/ 2>/dev/null
git commit -m "feat: multi-project 3-tier autopilot (vNEXT)

Project → Topic → Task; one agent/worktree/integrate per topic; fair
topic-level scheduling across armed projects. Spec:
docs/specs/2026-06-10-multi-project-autopilot.md"
```

---

## Devil's Advocate (plan-level): weakest assumption per task

| Task | Weakest assumption | Failure mode | Mitigation |
|---|---|---|---|
| 1 | All importers go through the re-export | A direct scalar reader survives | Task 10 grep gate |
| 2 | Migration adoption preserves sweep timing | `updated_at = now()` would freeze stale-claim recovery for 10 fresh minutes | Pinned: `test_backfill_wraps_flat_node_in_synthetic_topic` asserts updated_at copied |
| 3 | One state machine fits topics | A topic-only state needed later | `_TRANSITIONS` is shared data; additive extension |
| 4 | Legacy specs never contain a literal `## topic x:` heading | Such a flat spec parses as 3-tier | Acceptable: the string is unambiguous intent; mixed form rejected loudly |
| 5 | In-flight TOPIC count = load | Long-running topic under-weighted | By design: topic = thread = agent — the proxy EQUALS the budgeted resource (spec DA A4) |
| 6 | The transplanted per-topic tick body is faithful | Cross-project thread mis-binding → wrong-repo agent work; or double-dispatch via a broken claim | Pinned `test_each_thread_bound_to_its_topics_project`; node-level claim/sweep pins stay; adapted tick pins keep asserting cap/disarm/poison behavior |
| 7 | Completion gate runs BEFORE integrate | Gate after integrate would merge half-done topics | Pinned: refusal test asserts the integrate stub was NOT called; wire-up step names the seam |
| 8 | Existing suites never send-task to unbound armed-project threads | New refusal breaks an old test | Step 5 runs `test_cli_agents.py`; guard inert unless a project is armed |
| 9 | Topic-DAGs fit small viewports | Overflow in 80×67 third-pane | Smoke matrix gates; topic nodes are FEWER than task nodes were |
| 10 | Shared-DB hook-test failures are the documented isolation limitation | Masking a real new failure | Prove each failure on base before dismissing |

## Open questions

None — the contentious fork (execution model) was user-decided (hybrid);
everything else is resolved in the spec with rationale.
