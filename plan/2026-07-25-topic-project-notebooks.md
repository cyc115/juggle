# Topic & Project Notebooks — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `specs/2026-06-27-topic-project-notebooks.md` (approved DESIGN — WHAT & WHY).
**Design bar:** `specs/reviews/2026-06-27-p8-spec-da.md` — one model, one machine, no second write path, no dead abstraction.

**Goal:** Give every node a resumable working notebook — a generated markdown VIEW (Context / Tasks / Log) rendered from the unified `nodes` + `node_edges` graph plus ONE new append-only `node_notes` store, exposed as `juggle notebook show|append` and injected into the agent lifecycle by code (not prompts).

**Architecture:** The notebook is 95% a *render* of state that already exists. Exactly one new table (`node_notes`, append-only). One pure render function (dict → markdown string, zero DB, zero clock). One DB-collection layer that produces that dict. A materialized file that is *generated, never hand-edited* (atomic `os.replace`, so concurrent regenerates converge). Two lifecycle hooks that wire dispatch and completion. **No task write path** — checkbox state comes from `nodes.state` + `node_edges` and is mutated only through existing graph ops.

**Tech Stack:** Python 3.12 stdlib + sqlite3, argparse (declarative `Cmd`/`Arg` table), pytest. No new dependencies.

---

## Precondition: P8 — SATISFIED (2026-07-25)

The spec's §4 `BLOCKED-ON P8` precondition **is met**. Verified in this worktree:

```bash
uv run src/juggle_cli.py doctor --pre-p8-check --json
# => {"static": {"fail": 0, "refs": [], ...,"import_refs": 0},
#     "runtime": {"ready": false, "already_dropped": true, "reasons": ["already-dropped"]},
#     "pass": true}
```

Legacy `threads` / `graph_topics` / `graph_tasks` / `graph_edges` are DROPPED (Migration 55). This plan builds **directly** on `nodes` / `node_edges` with no dual-read, no shim. Step 9 updates the spec's status line so it stops telling future agents the feature is blocked.

---

## Global Constraints

Copied verbatim from `CLAUDE.md` / repo policy. Every task's requirements implicitly include this section.

- **Full suite green at EVERY commit.** `uv run pytest -q` (FULL suite — never a subset, never `-m "not slow"`). `make test` is the same scope, parallel.
- **Harness smoke gate (mandatory).** Before completing any step: full `pytest` green **plus** `uv run src/juggle_cli.py doctor --dry-run` against a tmp DB. Paste the summary line as evidence.
- **Architecture gate.** ≤300 lines per module (`scripts/loc_gate.py`, `LIMIT = 300`). The allowlist MAY ONLY SHRINK — never add an entry. Steps 1a/1b are pure-mechanical EXTRACT-first refactors precisely because two target files already sit at 296 and 300 lines.
- **Regression-pin gate.** Every fix/bug step adds a pinned test that fails RED on pre-fix code, names the incident in its docstring, and lives in the standard suite. Pins may never be deleted or weakened.
- **Tests: lean and high-signal.** Prefer a few strong tests over many weak ones.
- **POSIX-portable, mac + debian.** No `timeout`, no `grep -P`, no `date +%s%N` in any command this plan asks anyone to run.
- **Migration numbers are DB-reserved.** Run `juggle migration next` — never hand-pick by eyeballing `dbops/migration_*.py`.
- **Env required at import** (no defaults):
  ```bash
  export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"
  export JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
  ```
- **Never modify** `AGENTS.md`, `CLAUDE.md`, or `.codegraph` files.
- **Commit after every verified step.** Landing policy: ff-merge to `main` (this change adds a DB migration → per `CLAUDE.md` landing policy, **open a PR** for the migration step, or land the whole branch as one reviewed PR; do not silently ff-merge a schema change).
- **After modifying code:** run `graphify update .` (AST-only, no API cost).

---

## Decisions this plan locks (spec gaps resolved)

The spec left four things open or stated them against the pre-P8 vocabulary. This plan **resolves** them so a coder needs no clarification; each is re-raised in `--open-questions` for confirmation, and each is a one-line edit to reverse.

| # | Spec said | This plan does | Why |
|---|---|---|---|
| D1 | Topic = `nodes.kind='conversation'` (§3) | Render is **kind-agnostic**: notebook = any node + its **direct** `kind='task'` children. Project aggregation covers `kind IN ('topic','conversation')`. | Post-P8 reality (Migration 53): graph topics are `kind='topic'` nodes; chat threads are `kind='conversation'`. Both anchor work. A kind-agnostic render is the *one model* answer — no discriminator branching. |
| D2 | Four glyphs; failure states unassigned (§13) | Total glyph function: `[⊘]` blocked, `[!]` failure terminals, `[-]` cancelled/archived, `[x]` success/done, `[/]` active + `integrated-unlanded`, `[ ]` otherwise (incl. unknown states). | Spec §13 proposes `[!]`. A *total* function means a future state can never raise KeyError mid-render. |
| D3 | `~/.juggle/notebooks/<id>.md`, flagged (§13) | `settings["paths"]["notebooks_dir"]`, default `~/.claude/juggle/notebooks` (= `<data_dir>/notebooks`), env override `JUGGLE_NOTEBOOKS_DIR`. | Spec §13 itself recommends the plugin data-dir convention. Env override is what makes the file side effect test-isolatable. |
| D4 | Project = open topics, "open ⇔ not done/archived" (§8) | Excluded set = `DONE_ROLLUP_STATES ∪ ARCHIVED_STATES ∪ TERMINAL_SUCCESS_STATES ∪ CANCELLED_STATES` (from `dbops.terminal_states`). Failure terminals stay **visible**. | `verified`/`delivered` are finished work; a *live working set* that still lists them is noise. A failed topic is exactly what you want in the live view. |

Two more locked calls:

- **Dep-satisfaction set = `TERMINAL_SUCCESS_STATES` (`verified`, `delivered`) only** — identical to the scheduler's `dbops.db_graph_edges.unverified_deps`. The view must never claim a task is unblocked when the dispatcher thinks it is blocked.
- **Direct children only (depth 1)**, matching spec §6.3 ("`Tasks` lists direct child task-nodes"). No recursion ⇒ no unbounded-depth cost, and "deep subtree" is handled by definition.

---

## File Structure

**New modules** (all comfortably under the 300-line gate):

| Path | Responsibility |
|---|---|
| `src/dbops/schema_notes.py` | `CREATE_NODE_NOTES` DDL + index constants. Nothing else. (Mirrors `schema_spool.py` / `schema_runs.py`.) |
| `src/dbops/migration_76_node_notes.py` | Migration 76 — idempotent, presence-guarded, fail-soft. Wired in `migrations_tail.apply_tail_migrations`. |
| `src/dbops/node_notes.py` | `append_note` / `list_notes`. The only writer of `node_notes`. |
| `src/juggle_notebook_render.py` | **PURE**: `glyph_for`, `render_node`, `render_project`. No DB, no clock, no filesystem. |
| `src/juggle_notebook.py` | DB collection (`collect_node`, `collect_project`, `resolve_target`) + path resolution + atomic `materialize`. |
| `src/juggle_cmd_notebook.py` | CLI handlers `cmd_notebook_show`, `cmd_notebook_append`. |
| `src/juggle_notebook_hooks.py` | Lifecycle: `build_notebook_section` (pure) + `notebook_section_for_thread` + `record_completion`. |
| `src/juggle_dispatch_acquire.py` | *(Step 1a, mechanical)* `acquire_agent` + `_reuse_idle_agent`, moved out of `juggle_dispatch_core`. |
| `src/juggle_cmd_agents_fail.py` | *(Step 1b, mechanical)* `cmd_fail_agent`, moved out of `juggle_cmd_agents_complete`. |

**Modified:**

| Path | Change |
|---|---|
| `src/dbops/migrations_tail.py` | Register Migration 76. |
| `src/juggle_settings.py:82-88, ~358` | Add `paths.notebooks_dir`; add it to the expanduser key loop. |
| `src/juggle_cli_commands_misc.py` | Two `Cmd("notebook", …)` entries. |
| `src/juggle_dispatch_core.py` | Step 1a extraction; Step 7 injects the notebook section. |
| `src/juggle_cmd_agents_complete.py` | Step 1b extraction; Step 8 calls `record_completion`. |
| `src/juggle_spool_apply.py` | `notebook_append` event routing + `_NS_DEFAULTS` keys. |
| `tests/test_cli_verb_vocab.py` | Add `append` to `CLOSED_VERBS` (documented extension mechanism). |
| `tests/test_spool_apply_event_shape.py` | Add `notebook_append` to `WRITER_ARG_KEYS`. |
| `tests/test_agent_context_write_audit.py` | Add `(juggle_cmd_notebook, cmd_notebook_append)`. |
| `specs/2026-06-27-topic-project-notebooks.md` | Status line: BLOCKED-ON → IMPLEMENTED. |
| `TODO.md` | Mark done. |

**New tests:** `tests/test_node_notes.py`, `tests/test_notebook_render.py`, `tests/test_notebook_collect.py`, `tests/test_cmd_notebook.py`, `tests/test_notebook_hooks.py`.

---

## Step order and why

1a/1b are extract-first (the gate forces them). 2 lands the store. 3 is the pure render — no DB, so it is the cheapest thing to get exactly right. 4 feeds 3 from the DB. 5 exposes both on the CLI. 6 makes `append` legal from an agent process. 7 and 8 are the two hooks, independent of each other. 9 is docs.

---

# Task 1a: Extract `acquire_agent` out of `juggle_dispatch_core` (mechanical)

`src/juggle_dispatch_core.py` is **296 lines**. `LIMIT = 300`. Step 7 adds ~5 lines and would break the gate. Per the architecture gate: EXTRACT first, in its own behavior-free commit.

**Files:**
- Create: `src/juggle_dispatch_acquire.py`
- Modify: `src/juggle_dispatch_core.py` (remove `_reuse_idle_agent` + `acquire_agent`; re-export)
- Test: no new test — the existing suite IS the pin (`tests/test_dispatch_core.py`, `tests/test_dispatch_node.py`, `tests/test_dispatch_role.py`, `tests/test_clear_on_reuse.py`)

**Interfaces:**
- Consumes: nothing.
- Produces: `juggle_dispatch_core.acquire_agent` keeps its exact signature and stays a module attribute of `juggle_dispatch_core` (tests and `dispatch_node` resolve it there).

- [ ] **Step 1: Record the baseline so the refactor is provably behaviour-free**

```bash
cd /tmp/juggle-juggle-NB
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q tests/test_dispatch_core.py tests/test_dispatch_node.py tests/test_dispatch_role.py tests/test_clear_on_reuse.py
grep -rn "acquire_agent" src/ tests/ | sort
```
Expected: all four files PASS. Save the grep output — every call site must still resolve after the move.

- [ ] **Step 2: Create `src/juggle_dispatch_acquire.py`**

Move `_reuse_idle_agent` and `acquire_agent` **verbatim** (bodies unchanged, byte for byte) into this new module, with this header and the imports they need:

```python
"""juggle_dispatch_acquire — agent-pool acquisition for dispatch.

Owns: ``_reuse_idle_agent`` (CAS-claim a matching idle pane, warm reuse:
/clear + cd) and ``acquire_agent`` (pool walk + CAS-assign or spawn, then
thread -> background).
Must not own: prompt build / tmux send / ledger (juggle_dispatch_core), tick
orchestration, CLI arg parsing, sys.exit.

EXTRACTED MECHANICALLY from juggle_dispatch_core (2026-07-25, architecture LOC
gate): dispatch_core sat at 296/300 lines and the notebook dispatch hook needed
headroom. Bodies are byte-identical to the pre-move versions; juggle_dispatch_core
re-exports both names so every existing import and test monkeypatch
(`_core.acquire_agent`) keeps working unchanged.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import juggle_cmd_agents_common as _com
from juggle_agent_reuse_match import candidate_matches
from juggle_graph_dispatch import TASK_ROLE, CapacityError
from juggle_model_registry import is_poisoned_claude_model

_log = logging.getLogger("juggle-dispatch-core")

# ... _reuse_idle_agent and acquire_agent moved here VERBATIM ...
```

Note: `DEFAULT_WORKTREE_ROOT` stays in `juggle_dispatch_core` (tests monkeypatch `_core.DEFAULT_WORKTREE_ROOT`); `acquire_agent` does not use it.

- [ ] **Step 3: Re-export from `juggle_dispatch_core`**

Delete the two moved functions from `src/juggle_dispatch_core.py` and add this import near the existing `juggle_dispatch_literal` re-export:

```python
# acquire_agent/_reuse_idle_agent moved to juggle_dispatch_acquire (LOC gate,
# 2026-07-25). Re-exported so `from juggle_dispatch_core import acquire_agent`
# and test monkeypatches on `_core.acquire_agent` keep resolving here — and so
# dispatch_node() below, which calls the module-global name, stays interceptable.
from juggle_dispatch_acquire import (  # noqa: F401
    acquire_agent,
    _reuse_idle_agent,
)
```

Leave `dispatch_node` calling the bare name `acquire_agent(...)` — it now resolves to the re-exported module global, which is what a monkeypatch replaces.

Prune any import in `juggle_dispatch_core` that is now unused (`candidate_matches`, `is_poisoned_claude_model`, and `TASK_ROLE`/`CapacityError` **only if** nothing else in the file references them — `dispatch_node` still uses `TASK_ROLE`, so keep that one).

- [ ] **Step 4: Verify behaviour-free**

```bash
uv run python scripts/loc_gate.py --json | python3 -c "import json,sys; d=json.load(sys.stdin); print([f for f in d.get('offenders', []) if 'dispatch' in str(f)])"
uv run pytest -q
```
Expected: no dispatch offender; full suite PASS. `wc -l src/juggle_dispatch_core.py` should now report roughly 205.

- [ ] **Step 5: Commit**

```bash
git add src/juggle_dispatch_acquire.py src/juggle_dispatch_core.py
git commit -m "refactor(dispatch): extract acquire_agent to juggle_dispatch_acquire

Pure mechanical move — bodies byte-identical, both names re-exported from
juggle_dispatch_core. Frees LOC headroom (296/300) for the notebook dispatch hook.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
test "$(wc -l < src/juggle_dispatch_core.py)" -lt 260 && echo GATE-OK
uv run python -c "import sys; sys.path.insert(0,'src'); import juggle_dispatch_core as c; print(c.acquire_agent.__module__)"
# expects: juggle_dispatch_acquire
uv run pytest -q   # full suite green
```

---

# Task 1b: Extract `cmd_fail_agent` out of `juggle_cmd_agents_complete` (mechanical)

`src/juggle_cmd_agents_complete.py` is **exactly 300 lines** — zero headroom. Step 8 adds a hook call.

**Files:**
- Create: `src/juggle_cmd_agents_fail.py`
- Modify: `src/juggle_cmd_agents_complete.py`
- Test: existing suite is the pin (`tests/test_spool_agent_complete_fail_writes.py`, `tests/test_agent_context_write_audit.py`, `tests/test_complete_agent_wedge_fixes.py`)

**Interfaces:**
- Consumes: nothing.
- Produces: `juggle_cmd_agents_complete.cmd_fail_agent` remains importable (re-export) — `juggle_spool_apply._dispatch` and `tests/test_agent_context_write_audit.py` both import it from there.

- [ ] **Step 1: Baseline**

```bash
uv run pytest -q tests/test_spool_agent_complete_fail_writes.py tests/test_agent_context_write_audit.py tests/test_complete_agent_wedge_fixes.py
grep -rn "cmd_fail_agent" src/ tests/ | sort
```
Expected: PASS; save the call-site list.

- [ ] **Step 2: Create `src/juggle_cmd_agents_fail.py`**

Move `cmd_fail_agent` **verbatim** with this header:

```python
"""juggle_cmd_agents_fail — `juggle agent fail` handler.

Owns: cmd_fail_agent (transient -> leave running for retry; persistent ->
action item + close + graph fail).
Must not own: completion (juggle_cmd_agents_complete), spawn/release lifecycle,
worktree helpers, classifiers (juggle_cmd_agents_common).

EXTRACTED MECHANICALLY from juggle_cmd_agents_complete (2026-07-25, architecture
LOC gate): that module sat at exactly 300/300 and the notebook completion hook
needed headroom. Body byte-identical; juggle_cmd_agents_complete re-exports the
name so every existing import and test keeps working unchanged.
"""

import sys

import juggle_cmd_agents_common as _com
from dbops import event_kinds as _ek

# ... cmd_fail_agent moved here VERBATIM ...
```

- [ ] **Step 3: Re-export from `juggle_cmd_agents_complete`**

Delete `cmd_fail_agent` from `src/juggle_cmd_agents_complete.py`; add at the bottom of its import block:

```python
# cmd_fail_agent moved to juggle_cmd_agents_fail (LOC gate, 2026-07-25).
# Re-exported: juggle_spool_apply._dispatch and tests import it from HERE.
from juggle_cmd_agents_fail import cmd_fail_agent  # noqa: F401
```

- [ ] **Step 4: Verify**

```bash
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
uv run pytest -q
```
Expected: `LOC-OK`; full suite PASS.

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_agents_fail.py src/juggle_cmd_agents_complete.py
git commit -m "refactor(agents): extract cmd_fail_agent to juggle_cmd_agents_fail

Pure mechanical move — body byte-identical, name re-exported from
juggle_cmd_agents_complete. Frees LOC headroom (300/300) for the notebook
completion hook.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
test "$(wc -l < src/juggle_cmd_agents_complete.py)" -lt 240 && echo GATE-OK
uv run python -c "import sys; sys.path.insert(0,'src'); import juggle_cmd_agents_complete as m; print(m.cmd_fail_agent.__module__)"
# expects: juggle_cmd_agents_fail
uv run pytest -q
```

---

# Task 2: `node_notes` — the one new persistent store

**Files:**
- Create: `src/dbops/schema_notes.py`
- Create: `src/dbops/migration_76_node_notes.py`
- Create: `src/dbops/node_notes.py`
- Modify: `src/dbops/migrations_tail.py`
- Test: `tests/test_node_notes.py`

**Interfaces:**
- Consumes: `dbops.schema._now` (ISO-8601 UTC), `db._connect()`.
- Produces:
  - `dbops.schema_notes.CREATE_NODE_NOTES: str`, `CREATE_NODE_NOTES_INDEXES: list[str]`
  - `dbops.migration_76_node_notes.migrate_76_node_notes(conn: sqlite3.Connection) -> None`
  - `dbops.node_notes.append_note(db, node_id: str, body: str, who: str = "orch", *, ts: str | None = None) -> int` (returns the new row id)
  - `dbops.node_notes.list_notes(db, node_id: str) -> list[dict]` — keys `id, node_id, ts, who, body`, ordered by `id` ASC.

- [ ] **Step 1: Reserve the migration number**

```bash
uv run src/juggle_cli.py migration next
```
Expected: prints `76` (the DB allocator is authoritative — it read `76` on 2026-07-25 and the highest file on disk is `migration_75_*`). **If it prints something other than 76, use that number** and rename every `76` in this task accordingly.

- [ ] **Step 2: Write the failing test**

Create `tests/test_node_notes.py`:

```python
"""node_notes append-only store (spec 2026-06-27 §5.1).

The ONE new persistent state a notebook adds: a per-node narrative Log.
Append-only in v1 — no edit, no delete. The AUTOINCREMENT id defines a total
order independent of `ts` granularity, so the render is reproducible even for
notes written inside the same second.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops import node_notes  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402
from helpers.node_seed import seed_node  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "notes.db"))
    d.init_db()
    with d._connect() as conn:
        seed_node(conn, id="n1", kind="conversation", title="Topic one")
        conn.commit()
    return d


def test_table_exists_after_init_db(db):
    with db._connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "node_notes" in names


def test_append_returns_monotonic_ids_and_lists_in_append_order(db):
    first = node_notes.append_note(db, "n1", "first", who="orch")
    second = node_notes.append_note(db, "n1", "second", who="agent")
    assert second > first
    rows = node_notes.list_notes(db, "n1")
    assert [r["body"] for r in rows] == ["first", "second"]
    assert [r["who"] for r in rows] == ["orch", "agent"]
    assert [r["id"] for r in rows] == [first, second]


def test_append_is_not_dedup_identical_bodies_yield_two_rows(db):
    node_notes.append_note(db, "n1", "same", who="orch")
    node_notes.append_note(db, "n1", "same", who="orch")
    assert len(node_notes.list_notes(db, "n1")) == 2


def test_list_notes_is_scoped_to_the_node(db):
    with db._connect() as conn:
        seed_node(conn, id="n2", kind="conversation", title="Topic two")
        conn.commit()
    node_notes.append_note(db, "n1", "one", who="orch")
    node_notes.append_note(db, "n2", "two", who="orch")
    assert [r["body"] for r in node_notes.list_notes(db, "n1")] == ["one"]
    assert node_notes.list_notes(db, "missing") == []


def test_ts_is_iso8601_utc(db):
    node_notes.append_note(db, "n1", "x", who="orch")
    ts = node_notes.list_notes(db, "n1")[0]["ts"]
    assert ts.endswith("+00:00") or ts.endswith("Z")


def test_ordering_survives_same_second_writes(db):
    """Total order comes from the AUTOINCREMENT id, NOT from ts — three notes
    stamped with an identical ts still list in append order."""
    for body in ("a", "b", "c"):
        node_notes.append_note(db, "n1", body, who="orch", ts="2026-07-25T00:00:00+00:00")
    assert [r["body"] for r in node_notes.list_notes(db, "n1")] == ["a", "b", "c"]


def test_migration_is_idempotent(db):
    from dbops.migration_76_node_notes import migrate_76_node_notes

    node_notes.append_note(db, "n1", "survives", who="orch")
    with db._connect() as conn:
        migrate_76_node_notes(conn)
        migrate_76_node_notes(conn)
    assert [r["body"] for r in node_notes.list_notes(db, "n1")] == ["survives"]
```

- [ ] **Step 3: Run it to confirm RED**

```bash
uv run pytest -q tests/test_node_notes.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'dbops.node_notes'`.

- [ ] **Step 4: Create `src/dbops/schema_notes.py`**

```python
"""dbops.schema_notes — DDL for node_notes, the notebook's append-only Log store.

Owns: the CREATE TABLE / CREATE INDEX strings only.
Must not own: migration logic, query helpers, render logic.

Mirrors the existing append-only ledger convention (agent_runs /
dbops.schema_runs, spool_journal / dbops.schema_spool): one row per event,
monotonic PK, indexed by the owning entity.
"""
from __future__ import annotations

CREATE_NODE_NOTES = """
CREATE TABLE IF NOT EXISTS node_notes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,   -- monotonic => stable total order
  node_id   TEXT NOT NULL REFERENCES nodes(id),  -- the node this note belongs to
  ts        TEXT NOT NULL,                       -- ISO-8601 UTC append time
  who       TEXT NOT NULL,                       -- 'agent' | 'orch' (free text, v1)
  body      TEXT NOT NULL                        -- the narrative line(s)
);
"""

CREATE_NODE_NOTES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_node_notes_node ON node_notes(node_id);",
]
```

- [ ] **Step 5: Create `src/dbops/migration_76_node_notes.py`**

```python
"""Migration 76 (topic/project notebooks, spec 2026-06-27 §5.1) — additive
``node_notes`` table: the append-only narrative Log a notebook renders.

This is the ONE new persistent store the notebook feature adds. Everything else
a notebook shows (Context, Tasks) is rendered from existing nodes / node_edges.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration
58/70/74). Never rebuilds a table. Fresh DBs acquire it through the SAME path as
migrated DBs, because ``init_db`` always runs ``run_migrations`` — so the table
is created HERE and nowhere else (the Migration-58/spool_journal precedent).
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.schema_notes import CREATE_NODE_NOTES, CREATE_NODE_NOTES_INDEXES

_log = logging.getLogger(__name__)


def migrate_76_node_notes(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(CREATE_NODE_NOTES)
        for index_sql in CREATE_NODE_NOTES_INDEXES:
            conn.execute(index_sql)
        conn.commit()
        _log.info("Migration 76: node_notes table ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 76 (node_notes) skipped: %s", e)
```

- [ ] **Step 6: Register it in `src/dbops/migrations_tail.py`**

Append at the end of `apply_tail_migrations`, after the Migration 75 block:

```python
    # Migration 76 (topic/project notebooks): additive node_notes table — the
    # append-only narrative Log the notebook render reads.
    from dbops.migration_76_node_notes import migrate_76_node_notes
    migrate_76_node_notes(conn)
```

- [ ] **Step 7: Create `src/dbops/node_notes.py`**

```python
"""dbops.node_notes — the append-only notebook Log store (Migration 76).

Owns: the ONLY writer of ``node_notes`` (``append_note``) and its reader
(``list_notes``).
Must not own: rendering (juggle_notebook_render), path/file materialization
(juggle_notebook), CLI parsing (juggle_cmd_notebook).

v1 contract (spec 2026-06-27 §5.1): APPEND ONLY. There is deliberately no
update and no delete — a note, once written, is permanent. Total order is the
AUTOINCREMENT ``id``, never ``ts``, so two notes written inside the same second
still render deterministically.
"""
from __future__ import annotations

from dbops.schema import _now

# Free-text in v1; these are the two conventional values (spec §7.2).
WHO_AGENT = "agent"
WHO_ORCH = "orch"


def append_note(db, node_id: str, body: str, who: str = WHO_ORCH,
                *, ts: str | None = None) -> int:
    """Append one Log row to ``node_id``. Returns the new row id.

    ``body`` is stored VERBATIM (never trimmed to a length cap — unlike
    nodes.learnings, the Log is narrative). ``ts`` is injectable so tests can
    pin same-second ordering; production always passes None.
    """
    with db._connect() as conn:
        cur = conn.execute(
            "INSERT INTO node_notes (node_id, ts, who, body) VALUES (?,?,?,?)",
            (node_id, ts or _now(), who, body),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_notes(db, node_id: str) -> list[dict]:
    """Every note for ``node_id``, oldest -> newest by append order (id)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, node_id, ts, who, body FROM node_notes "
            "WHERE node_id=? ORDER BY id",
            (node_id,),
        ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 8: Run the test to verify it passes**

```bash
uv run pytest -q tests/test_node_notes.py
```
Expected: 7 passed.

- [ ] **Step 9: Full suite + doctor smoke**

```bash
uv run pytest -q
JUGGLE_DB_PATH=/tmp/nb-doctor.db uv run src/juggle_cli.py doctor --dry-run
```
Expected: full suite PASS; doctor dry-run exits 0.

- [ ] **Step 10: Commit**

```bash
git add src/dbops/schema_notes.py src/dbops/migration_76_node_notes.py \
        src/dbops/node_notes.py src/dbops/migrations_tail.py tests/test_node_notes.py
git commit -m "feat(notebook): add node_notes append-only store (Migration 76)

The ONE new persistent state topic/project notebooks add (spec
2026-06-27 §5.1). Append-only in v1: no edit, no delete. Total order is the
AUTOINCREMENT id, not ts, so same-second notes render deterministically.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_node_notes.py            # 7 passed
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
```

---

# Task 3: The pure render function

The heart of the feature and the cheapest thing to get exactly right: a function from a plain dict to a markdown string. **No DB, no clock, no filesystem** — so "render twice → byte-identical" is true by construction, and every glyph case is a one-line unit test.

**Files:**
- Create: `src/juggle_notebook_render.py`
- Test: `tests/test_notebook_render.py`

**Interfaces:**
- Consumes: `dbops.terminal_states` (`ACTIVE_STATES`, `TERMINAL_SUCCESS_STATES`, `DONE_ROLLUP_STATES`, `ASYNC_PENDING_STATES`, `CANCELLED_STATES`, `ARCHIVED_STATES`, `FAILURE_TERMINAL_STATES`).
- Produces:
  - `glyph_for(state: str, *, blocked: bool) -> str` — returns one of `"⊘" "!" "-" "x" "/" " "`.
  - `render_node(data: dict) -> str`
  - `render_project(data: dict) -> str`
  - `EMPTY = "_(none yet)_"`

  `render_node` consumes exactly this dict (produced by Task 4):
  ```python
  {
    "node_id": str, "kind": str, "state": str, "project_id": str | None,
    "title": str,
    "context": {"objective": str, "last_user_intent": str},
    "tasks": [{"id": str, "title": str, "state": str,
               "glyph": str, "blocked_by": [str], "blocked_by_titles": [str]}],
    "log": [{"id": int, "ts": str, "who": str, "body": str}],
  }
  ```
  `render_project` consumes:
  ```python
  {"project_id": str, "name": str, "objective": str, "topics": [<node dict>, ...]}
  ```

- [ ] **Step 1: Write the failing test**

Create `tests/test_notebook_render.py`:

```python
"""Pure notebook render (spec 2026-06-27 §6). The §6.3 example IS the contract.

Render is a pure function of its input dict — no DB, no clock, no filesystem —
so determinism ("render twice, byte-identical") holds by construction and every
glyph case is a single unit assertion.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_notebook_render import EMPTY, glyph_for, render_node, render_project  # noqa: E402


def _task(tid, title, state, glyph, blocked_by=(), blocked_titles=()):
    return {"id": tid, "title": title, "state": state, "glyph": glyph,
            "blocked_by": list(blocked_by), "blocked_by_titles": list(blocked_titles)}


FULL = {
    "node_id": "4f3c", "kind": "conversation", "state": "open",
    "project_id": "webapp", "title": "Add OAuth login",
    "context": {
        "objective": "Let users sign in with Google and GitHub OAuth instead of passwords.",
        "last_user_intent": "prioritise Google first; GitHub can follow in a later topic.",
    },
    "tasks": [
        _task("t1", "Add OAuth provider config", "verified", "x"),
        _task("t2", "Implement Google callback handler", "running", "/"),
        _task("t3", "Implement GitHub callback handler", "open", "⊘",
              ["t1"], ["Add OAuth provider config"]),
        _task("t4", "Write integration tests", "open", " "),
    ],
    "log": [
        {"id": 1, "ts": "2026-06-27T14:02:11Z", "who": "orch",
         "body": "Topic created; scoped to Google-first."},
        {"id": 2, "ts": "2026-06-27T15:30:44Z", "who": "agent",
         "body": "Provider config landed; callback handler in progress."},
    ],
}


# ── §6.2 glyph derivation (first match wins) ──────────────────────────────────

@pytest.mark.parametrize("state,blocked,expected", [
    ("open", True, "⊘"),
    ("ready", True, "⊘"),
    ("verified", False, "x"),
    ("delivered", False, "x"),
    ("done", False, "x"),
    ("dispatching", False, "/"),
    ("running", False, "/"),
    ("integrating", False, "/"),
    ("integrated-unlanded", False, "/"),
    ("open", False, " "),
    ("ready", False, " "),
    ("failed-exec", False, "!"),
    ("failed-verify", False, "!"),
    ("failed-integration", False, "!"),
    ("blocked-failed", False, "!"),
    ("cancelled", False, "-"),
    ("archived", False, "-"),
])
def test_glyph_for_each_state(state, blocked, expected):
    assert glyph_for(state, blocked=blocked) == expected


def test_glyph_blocked_never_masks_a_terminal_or_active_state():
    """`blocked` only applies to a not-yet-started task; a running or verified
    task that still has an unmet dep renders by its own state."""
    assert glyph_for("running", blocked=True) == "/"
    assert glyph_for("verified", blocked=True) == "x"
    assert glyph_for("failed-exec", blocked=True) == "!"


def test_glyph_for_is_total_unknown_state_falls_back_to_pending():
    """A state added by a future migration must never raise mid-render."""
    assert glyph_for("some-future-state", blocked=False) == " "


# ── §6.3 exact rendered format ────────────────────────────────────────────────

def test_render_node_matches_the_spec_contract_example():
    assert render_node(FULL) == (
        "# Add OAuth login\n"
        "\n"
        "_node: 4f3c · kind: conversation · state: open · project: webapp_\n"
        "\n"
        "## Context\n"
        "\n"
        "Let users sign in with Google and GitHub OAuth instead of passwords.\n"
        "\n"
        "Intent: prioritise Google first; GitHub can follow in a later topic.\n"
        "\n"
        "## Tasks\n"
        "\n"
        "- [x] Add OAuth provider config\n"
        "- [/] Implement Google callback handler\n"
        "- [⊘] Implement GitHub callback handler (waiting on: Add OAuth provider config)\n"
        "- [ ] Write integration tests\n"
        "\n"
        "## Log\n"
        "\n"
        "- 2026-06-27T14:02:11Z · orch: Topic created; scoped to Google-first.\n"
        "- 2026-06-27T15:30:44Z · agent: Provider config landed; callback handler in progress.\n"
    )


def test_render_is_deterministic_byte_identical_twice():
    assert render_node(FULL) == render_node(FULL)


def test_render_node_with_no_children_and_no_notes_keeps_stable_structure():
    bare = dict(FULL, tasks=[], log=[], context={"objective": "", "last_user_intent": ""},
                project_id=None)
    out = render_node(bare)
    assert "## Context\n\n" + EMPTY in out
    assert "## Tasks\n\n" + EMPTY in out
    assert "## Log\n\n" + EMPTY in out
    assert "· project: INBOX_" in out
    assert "Intent:" not in out          # omitted entirely when empty (§6.3)


def test_render_node_omits_intent_line_when_blank_but_keeps_objective():
    d = dict(FULL, context={"objective": "Do the thing.", "last_user_intent": ""})
    out = render_node(d)
    assert "Do the thing." in out
    assert "Intent:" not in out


def test_blocked_row_names_every_blocking_dep():
    d = dict(FULL, tasks=[_task("t3", "GitHub handler", "open", "⊘",
                                ["t1", "t2"], ["Provider config", "Google handler"])])
    assert "- [⊘] GitHub handler (waiting on: Provider config, Google handler)\n" in render_node(d)


def test_multiline_note_body_is_indented_not_flattened():
    d = dict(FULL, log=[{"id": 1, "ts": "T", "who": "agent", "body": "line one\nline two"}])
    out = render_node(d)
    assert "- T · agent: line one\n  line two\n" in out


# ── §8 project aggregation render ─────────────────────────────────────────────

def test_render_project_concatenates_topic_sections_under_a_project_header():
    proj = {"project_id": "P1", "name": "Webapp", "objective": "Ship auth.",
            "topics": [FULL, dict(FULL, node_id="9a1b", title="Second topic",
                                  tasks=[], log=[])]}
    out = render_project(proj)
    assert out.startswith("# Project Webapp\n\n_project: P1 · topics: 2 open_\n\nShip auth.\n")
    assert "# Add OAuth login" in out
    assert "# Second topic" in out
    assert out.count("\n---\n") == 2      # one separator before each topic section


def test_render_project_with_zero_open_topics():
    proj = {"project_id": "P1", "name": "Webapp", "objective": "", "topics": []}
    out = render_project(proj)
    assert "_project: P1 · topics: 0 open_" in out
    assert EMPTY in out
```

- [ ] **Step 2: Run it to confirm RED**

```bash
uv run pytest -q tests/test_notebook_render.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_notebook_render'`.

- [ ] **Step 3: Create `src/juggle_notebook_render.py`**

```python
"""juggle_notebook_render — PURE node-subtree -> markdown render (spec
2026-06-27 §6). The §6.3 example IS the contract.

Owns: glyph derivation and the exact markdown format for a node notebook and a
project aggregate.
Must not own: any DB read (juggle_notebook), file materialization
(juggle_notebook), CLI parsing (juggle_cmd_notebook).

PURITY IS THE POINT: no DB handle, no clock, no filesystem, no randomness. Given
the same input dict this always returns the same string, so "render twice ->
byte-identical" is true by construction and there is no concurrency/clobber risk
on the materialized file (DA3): the file is generated, never hand-edited, and
concurrent regenerates converge on identical bytes.
"""
from __future__ import annotations

from dbops.terminal_states import (
    ACTIVE_STATES,
    ARCHIVED_STATES,
    ASYNC_PENDING_STATES,
    CANCELLED_STATES,
    DONE_ROLLUP_STATES,
    FAILURE_TERMINAL_STATES,
    TERMINAL_SUCCESS_STATES,
)

# Empty-section placeholder — emitted so section structure stays stable and
# diffable whether or not there is content (§6.3).
EMPTY = "_(none yet)_"

# Glyph vocabulary (§6.2 + the §13 failure marker this plan resolves as D2).
GLYPH_BLOCKED = "⊘"   # waiting on an unmet dependency
GLYPH_FAILED = "!"    # a failure terminal
GLYPH_DROPPED = "-"   # cancelled / archived — closed without success
GLYPH_DONE = "x"      # verified / delivered / done
GLYPH_ACTIVE = "/"    # dispatching / running / integrating / integrated-unlanded
GLYPH_PENDING = " "   # open / ready (and any state this module has not met)

# "A dep is satisfied" uses EXACTLY the scheduler's set
# (dbops.db_graph_edges.unverified_deps: NOT IN ('verified','delivered')), so the
# rendered checkbox can never claim a task is unblocked when the dispatcher
# considers it blocked. One graph, one answer.
DEP_SATISFIED_STATES = TERMINAL_SUCCESS_STATES

# Only a not-yet-started task can render as blocked. A running/verified/failed
# task renders by its own state even if a dep is somehow unmet.
_BLOCKABLE_STATES = frozenset({"open", "ready"})

_DONE_STATES = TERMINAL_SUCCESS_STATES | DONE_ROLLUP_STATES
_ACTIVE_GLYPH_STATES = ACTIVE_STATES | ASYNC_PENDING_STATES
_DROPPED_STATES = CANCELLED_STATES | ARCHIVED_STATES


def glyph_for(state: str, *, blocked: bool) -> str:
    """The checkbox glyph for ``state`` (§6.2 order — first match wins).

    TOTAL by design: an unrecognised state (e.g. one a future migration adds)
    falls back to pending rather than raising mid-render.
    """
    if blocked and state in _BLOCKABLE_STATES:
        return GLYPH_BLOCKED
    if state in FAILURE_TERMINAL_STATES:
        return GLYPH_FAILED
    if state in _DROPPED_STATES:
        return GLYPH_DROPPED
    if state in _DONE_STATES:
        return GLYPH_DONE
    if state in _ACTIVE_GLYPH_STATES:
        return GLYPH_ACTIVE
    return GLYPH_PENDING


def _note_lines(note: dict) -> list[str]:
    """One Log bullet. A multi-line body keeps its lines, continuation-indented
    by two spaces, so nothing is silently flattened or truncated."""
    body = str(note.get("body") or "")
    head, *rest = body.split("\n")
    lines = [f"- {note.get('ts', '')} · {note.get('who', '')}: {head}"]
    lines += [f"  {line}" for line in rest]
    return lines


def _task_line(task: dict) -> str:
    glyph = task.get("glyph") or GLYPH_PENDING
    line = f"- [{glyph}] {task.get('title', '')}"
    if glyph == GLYPH_BLOCKED:
        names = task.get("blocked_by_titles") or task.get("blocked_by") or []
        if names:
            line += f" (waiting on: {', '.join(names)})"
    return line


def _section(header: str, lines: list[str]) -> list[str]:
    return [header, "", *(lines or [EMPTY]), ""]


def render_node(data: dict) -> str:
    """Render one node's notebook: H1 + metadata line + Context / Tasks / Log."""
    ctx = data.get("context") or {}
    objective = (ctx.get("objective") or "").strip()
    intent = (ctx.get("last_user_intent") or "").strip()

    context_lines: list[str] = []
    if objective:
        context_lines.append(objective)
    if intent:
        if context_lines:
            context_lines.append("")
        context_lines.append(f"Intent: {intent}")

    out: list[str] = [
        f"# {data.get('title', '')}",
        "",
        "_node: {id} · kind: {kind} · state: {state} · project: {project}_".format(
            id=data.get("node_id", ""),
            kind=data.get("kind", ""),
            state=data.get("state", ""),
            project=data.get("project_id") or "INBOX",
        ),
        "",
    ]
    out += _section("## Context", context_lines)
    out += _section("## Tasks", [_task_line(t) for t in data.get("tasks") or []])

    log_lines: list[str] = []
    for note in data.get("log") or []:
        log_lines += _note_lines(note)
    out += _section("## Log", log_lines)

    # Exactly one trailing newline: drop the final blank produced by _section.
    return "\n".join(out[:-1]) + "\n"


def render_project(data: dict) -> str:
    """Render the project aggregate: header, then one section per open topic
    (§8). On-read only — no project file is ever materialized."""
    topics = data.get("topics") or []
    objective = (data.get("objective") or "").strip()
    out = [
        f"# Project {data.get('name', '')}",
        "",
        f"_project: {data.get('project_id', '')} · topics: {len(topics)} open_",
        "",
        objective or EMPTY,
        "",
    ]
    body = "\n".join(out)
    if not topics:
        # Header + objective only. The objective slot already carries EMPTY when
        # the project has none, so no second placeholder is emitted.
        return body.rstrip("\n") + "\n"
    return body + "\n---\n\n" + "\n---\n\n".join(render_node(t) for t in topics)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest -q tests/test_notebook_render.py
```
Expected: all PASS. If the byte-exact contract test fails, fix the **renderer**, not the expectation — that expectation is transcribed from spec §6.3.

- [ ] **Step 5: Full suite + commit**

```bash
uv run pytest -q
git add src/juggle_notebook_render.py tests/test_notebook_render.py
git commit -m "feat(notebook): pure node-subtree -> markdown render

Deterministic string function (no DB, no clock, no filesystem) implementing
spec 2026-06-27 §6. Glyph derivation is TOTAL — an unknown future state falls
back to pending instead of raising mid-render. Dep-satisfaction reuses the
scheduler's set so the view can never disagree with the dispatcher.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_notebook_render.py
# Purity, mechanically checked: the module imports no DB/clock/fs surface.
! grep -nE "^(import|from) (os|pathlib|sqlite3|datetime|time|random)\b" src/juggle_notebook_render.py && echo PURE-OK
```

---

# Task 4: DB collection, path config, atomic materialization

**Files:**
- Create: `src/juggle_notebook.py`
- Modify: `src/juggle_settings.py` (add `paths.notebooks_dir` + expanduser key)
- Test: `tests/test_notebook_collect.py`

**Interfaces:**
- Consumes: `dbops.node_notes.list_notes`, `juggle_notebook_render.{glyph_for, render_node, render_project, DEP_SATISFIED_STATES}`, `db._connect()`, `db.get_project`.
- Produces:
  - `collect_node(db, node_id: str) -> dict | None` — the exact dict `render_node` consumes.
  - `collect_project(db, project_id: str) -> dict | None` — the exact dict `render_project` consumes.
  - `resolve_target(db, ident: str) -> tuple[str, str] | None` — `("project", pid)` or `("node", node_id)`.
  - `notebooks_dir() -> pathlib.Path`
  - `notebook_path(node_id: str) -> pathlib.Path`
  - `materialize(node_id: str, markdown: str) -> pathlib.Path`
  - `NOTEBOOKS_DIR_ENV = "JUGGLE_NOTEBOOKS_DIR"`
  - `PROJECT_CLOSED_STATES: frozenset[str]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notebook_collect.py`:

```python
"""Notebook DB collection + materialization (spec 2026-06-27 §6.4, §7.3, §8).

Collection turns graph state into the exact dict the pure renderer consumes;
materialization writes the render to a configurable path, atomically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_notebook as nb  # noqa: E402
from dbops import node_notes  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402
from helpers.node_seed import seed_node  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "nb.db"))
    d.init_db()
    return d


@pytest.fixture(autouse=True)
def notebooks_tmp(tmp_path, monkeypatch):
    """Never write into the real ~/.claude/juggle/notebooks from a test."""
    target = tmp_path / "notebooks"
    monkeypatch.setenv(nb.NOTEBOOKS_DIR_ENV, str(target))
    return target


def _seed_topic_with_children(db, project_id=None):
    with db._connect() as conn:
        seed_node(conn, id="topic1", kind="topic", title="Add OAuth login",
                  state="open", project_id=project_id,
                  objective="Let users sign in with OAuth.",
                  last_user_intent="Google first.")
        seed_node(conn, id="t1", kind="task", title="Provider config",
                  state="verified", parent_id="topic1", project_id=project_id)
        seed_node(conn, id="t2", kind="task", title="Google handler",
                  state="running", parent_id="topic1", project_id=project_id)
        seed_node(conn, id="t3", kind="task", title="GitHub handler",
                  state="open", parent_id="topic1", project_id=project_id)
        seed_node(conn, id="t4", kind="task", title="Integration tests",
                  state="open", parent_id="topic1", project_id=project_id)
        # t3 depends on t2 (running -> unmet) => blocked. t4 depends on t1
        # (verified -> met) => pending, not blocked.
        conn.executemany(
            "INSERT INTO node_edges (node_id, depends_on_id, kind) VALUES (?,?,'dep')",
            [("t3", "t2"), ("t4", "t1")],
        )
        conn.commit()


def test_collect_node_returns_none_for_unknown_id(db):
    assert nb.collect_node(db, "nope") is None


def test_collect_node_context_comes_from_objective_and_last_user_intent(db):
    _seed_topic_with_children(db)
    data = nb.collect_node(db, "topic1")
    assert data["title"] == "Add OAuth login"
    assert data["kind"] == "topic"
    assert data["context"] == {
        "objective": "Let users sign in with OAuth.",
        "last_user_intent": "Google first.",
    }


def test_collect_node_glyphs_and_blocked_by_match_the_graph(db):
    _seed_topic_with_children(db)
    tasks = {t["id"]: t for t in nb.collect_node(db, "topic1")["tasks"]}
    assert tasks["t1"]["glyph"] == "x"
    assert tasks["t2"]["glyph"] == "/"
    assert tasks["t3"]["glyph"] == "⊘"
    assert tasks["t3"]["blocked_by"] == ["t2"]
    assert tasks["t3"]["blocked_by_titles"] == ["Google handler"]
    assert tasks["t4"]["glyph"] == " "       # dep verified => unblocked
    assert tasks["t4"]["blocked_by"] == []


def test_collect_node_task_order_is_deterministic(db):
    _seed_topic_with_children(db)
    ids = [t["id"] for t in nb.collect_node(db, "topic1")["tasks"]]
    assert ids == sorted(ids)               # created_at ties resolve by id


def test_collect_node_lists_only_direct_task_children(db):
    _seed_topic_with_children(db)
    with db._connect() as conn:
        seed_node(conn, id="g1", kind="task", title="Grandchild", parent_id="t1")
        seed_node(conn, id="c1", kind="conversation", title="Chat", parent_id="topic1")
        conn.commit()
    ids = [t["id"] for t in nb.collect_node(db, "topic1")["tasks"]]
    assert "g1" not in ids and "c1" not in ids


def test_collect_node_with_no_children_and_no_notes(db):
    with db._connect() as conn:
        seed_node(conn, id="lonely", kind="conversation", title="Alone")
        conn.commit()
    data = nb.collect_node(db, "lonely")
    assert data["tasks"] == [] and data["log"] == []


def test_collect_node_log_is_append_ordered(db):
    _seed_topic_with_children(db)
    node_notes.append_note(db, "topic1", "first", who="orch")
    node_notes.append_note(db, "topic1", "second", who="agent")
    assert [n["body"] for n in nb.collect_node(db, "topic1")["log"]] == ["first", "second"]


# ── §8 project aggregation ────────────────────────────────────────────────────

def test_collect_project_includes_open_topics_and_excludes_finished_ones(db):
    pid = db.create_project("Webapp", "Ship auth.")
    _seed_topic_with_children(db, project_id=pid)
    with db._connect() as conn:
        seed_node(conn, id="topic_done", kind="topic", title="Old", state="done",
                  project_id=pid)
        seed_node(conn, id="topic_verified", kind="topic", title="Shipped",
                  state="verified", project_id=pid)
        seed_node(conn, id="topic_failed", kind="topic", title="Broken",
                  state="failed-verify", project_id=pid)
        conn.commit()
    ids = [t["node_id"] for t in nb.collect_project(db, pid)["topics"]]
    assert "topic1" in ids
    assert "topic_failed" in ids            # failures stay in the live view
    assert "topic_done" not in ids and "topic_verified" not in ids


def test_collect_project_writes_no_materialized_project_file(db, notebooks_tmp):
    pid = db.create_project("Webapp", "Ship auth.")
    _seed_topic_with_children(db, project_id=pid)
    nb.collect_project(db, pid)
    assert not (notebooks_tmp / f"{pid}.md").exists()


def test_collect_project_returns_none_for_unknown_project(db):
    assert nb.collect_project(db, "P404") is None


def test_inbox_aggregates_null_project_topics(db):
    _seed_topic_with_children(db, project_id=None)
    ids = [t["node_id"] for t in nb.collect_project(db, "INBOX")["topics"]]
    assert "topic1" in ids


# ── §6.4 materialized file ────────────────────────────────────────────────────

def test_notebooks_dir_honours_env_override(notebooks_tmp):
    assert nb.notebooks_dir() == notebooks_tmp


def test_materialize_creates_missing_directories(db, notebooks_tmp):
    assert not notebooks_tmp.exists()
    path = nb.materialize("topic1", "# hello\n")
    assert path.read_text() == "# hello\n"
    assert path == notebooks_tmp / "topic1.md"


def test_materialize_overwrites_wholesale_never_appends(db, notebooks_tmp):
    nb.materialize("topic1", "# first\n")
    path = nb.materialize("topic1", "# second\n")
    assert path.read_text() == "# second\n"


def test_materialize_leaves_no_temp_files_behind(db, notebooks_tmp):
    nb.materialize("topic1", "# x\n")
    assert [p.name for p in notebooks_tmp.iterdir()] == ["topic1.md"]


# ── target resolution ─────────────────────────────────────────────────────────

def test_resolve_target_prefers_project_then_node(db):
    pid = db.create_project("Webapp", "Ship auth.")
    _seed_topic_with_children(db)
    assert nb.resolve_target(db, pid) == ("project", pid)
    assert nb.resolve_target(db, "topic1") == ("node", "topic1")
    assert nb.resolve_target(db, "nothing-here") is None


def test_resolve_target_accepts_a_conversation_user_label(db):
    tid = db.create_thread("Chat topic", session_id="s")
    label = db.get_thread(tid).get("user_label")
    if label:                                # labels are allocated off a wheel
        assert nb.resolve_target(db, label) == ("node", tid)
```

- [ ] **Step 2: Run it to confirm RED**

```bash
uv run pytest -q tests/test_notebook_collect.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_notebook'`.

- [ ] **Step 3: Add the settings key**

In `src/juggle_settings.py`, inside `DEFAULTS["paths"]`, add the entry after `digest_log_dir`:

```python
        # Notebook render output (spec 2026-06-27 §6.4 / §13): generated markbook
        # files live beside the DB under the plugin data dir — NOT ~/.juggle —
        # so a notebook and the graph it renders share one volume.
        "notebooks_dir": "~/.claude/juggle/notebooks",
```

And extend the expanduser loop (currently `for key in ("data_dir", "config_dir", "digest_log_dir")`):

```python
    for key in ("data_dir", "config_dir", "digest_log_dir", "notebooks_dir"):
        settings["paths"][key] = str(Path(settings["paths"][key]).expanduser())
```

- [ ] **Step 4: Create `src/juggle_notebook.py`**

```python
"""juggle_notebook — DB collection + file materialization for notebooks.

Owns: turning graph state (nodes / node_edges / node_notes) into the plain dict
the PURE renderer consumes, resolving a user-supplied id to a project or node,
and writing the generated markdown to its configured path.
Must not own: the markdown format or glyph rules (juggle_notebook_render), the
notes store (dbops.node_notes), CLI parsing (juggle_cmd_notebook).

READ + ONE DERIVED FILE ONLY. This module never writes a node, an edge, or a
task state — checkboxes are a RENDERING of the graph and there is no second
write path (spec §7.4). The only write is the generated .md file, which is
overwritten wholesale via a tmp file + os.replace, so concurrent regenerates
converge atomically and a reader never sees a torn file.
"""
from __future__ import annotations

import os
from pathlib import Path

from dbops import node_notes
from dbops.schema import INBOX_PROJECT_ID
from dbops.terminal_states import (
    ARCHIVED_STATES,
    CANCELLED_STATES,
    DONE_ROLLUP_STATES,
    TERMINAL_SUCCESS_STATES,
)
from juggle_notebook_render import DEP_SATISFIED_STATES, glyph_for

NOTEBOOKS_DIR_ENV = "JUGGLE_NOTEBOOKS_DIR"

# Topic kinds that can anchor a notebook in a project aggregate. Post-P8 a graph
# topic is kind='topic' and a chat topic is kind='conversation'; both are real
# workstreams, so both aggregate (plan decision D1).
PROJECT_TOPIC_KINDS = ("topic", "conversation")

# A project notebook is the LIVE working set (§8): drop anything finished,
# cancelled or archived. Failure terminals stay VISIBLE — a failed topic is
# exactly what a resumable working view must surface (plan decision D4).
PROJECT_CLOSED_STATES = frozenset(
    DONE_ROLLUP_STATES | ARCHIVED_STATES | TERMINAL_SUCCESS_STATES | CANCELLED_STATES
)


# ── paths / materialization (§6.4) ────────────────────────────────────────────

def notebooks_dir() -> Path:
    """Configured notebook output directory.

    Resolution order: ``JUGGLE_NOTEBOOKS_DIR`` env (the test/agent override) ->
    ``settings["paths"]["notebooks_dir"]`` -> ``~/.claude/juggle/notebooks``.
    """
    env = os.environ.get(NOTEBOOKS_DIR_ENV)
    if env:
        return Path(env).expanduser()
    from juggle_settings import get_settings

    configured = get_settings()["paths"].get("notebooks_dir")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "juggle" / "notebooks"


def notebook_path(node_id: str) -> Path:
    return notebooks_dir() / f"{node_id}.md"


def materialize(node_id: str, markdown: str) -> Path:
    """Write ``markdown`` to the node's notebook path and return it.

    Creates missing parents (a fresh machine has no notebooks dir yet) and
    replaces atomically: two processes regenerating at once produce identical
    bytes, and no reader ever observes a partial file.
    """
    path = notebook_path(node_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(markdown, encoding="utf-8")
    os.replace(tmp, path)
    return path


# ── collection ────────────────────────────────────────────────────────────────

def _child_tasks(db, node_id: str) -> list[dict]:
    """Direct kind='task' children with dep-derived blocked state.

    Two queries total (children, then all their dep edges joined to the dep's
    state+title) — never one per child, so a wide topic stays cheap.
    """
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, title, state FROM nodes "
            "WHERE kind='task' AND parent_id=? ORDER BY created_at, id",
            (node_id,),
        ).fetchall()
        children = [dict(r) for r in rows]
        if not children:
            return []
        placeholders = ",".join("?" * len(children))
        edges = conn.execute(
            "SELECT e.node_id AS child, e.depends_on_id AS dep, "
            "       d.state AS dep_state, d.title AS dep_title "
            "FROM node_edges e JOIN nodes d ON d.id = e.depends_on_id "
            f"WHERE e.kind='dep' AND e.node_id IN ({placeholders}) "
            "ORDER BY e.depends_on_id",
            tuple(c["id"] for c in children),
        ).fetchall()

    unmet: dict[str, list[tuple[str, str]]] = {c["id"]: [] for c in children}
    for e in edges:
        if e["dep_state"] not in DEP_SATISFIED_STATES:
            unmet[e["child"]].append((e["dep"], e["dep_title"] or e["dep"]))

    tasks = []
    for child in children:
        blockers = unmet[child["id"]]
        tasks.append({
            "id": child["id"],
            "title": child["title"],
            "state": child["state"],
            "glyph": glyph_for(child["state"], blocked=bool(blockers)),
            "blocked_by": [b[0] for b in blockers],
            "blocked_by_titles": [b[1] for b in blockers],
        })
    return tasks


def collect_node(db, node_id: str) -> dict | None:
    """The render dict for one node, or None when the node does not exist."""
    with db._connect() as conn:
        row = conn.execute(
            "SELECT id, kind, title, state, project_id, objective, last_user_intent "
            "FROM nodes WHERE id=?",
            (node_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "node_id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "project_id": row["project_id"],
        "title": row["title"],
        "context": {
            "objective": row["objective"] or "",
            "last_user_intent": row["last_user_intent"] or "",
        },
        "tasks": _child_tasks(db, node_id),
        "log": node_notes.list_notes(db, node_id),
    }


def collect_project(db, project_id: str) -> dict | None:
    """The render dict for a project aggregate (§8) — ON-READ, always consistent
    with the graph. Nothing is materialized and nothing is cached, so there is
    no staleness to invalidate."""
    project = db.get_project(project_id)
    if project is None and project_id != INBOX_PROJECT_ID:
        return None
    kinds = ",".join("?" * len(PROJECT_TOPIC_KINDS))
    closed = sorted(PROJECT_CLOSED_STATES)
    states = ",".join("?" * len(closed))
    if project_id == INBOX_PROJECT_ID:
        scope, params = "(project_id IS NULL OR project_id=?)", (INBOX_PROJECT_ID,)
    else:
        scope, params = "project_id=?", (project_id,)
    with db._connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM nodes WHERE kind IN ({kinds}) AND parent_id IS NULL "
            f"AND {scope} AND state NOT IN ({states}) ORDER BY created_at, id",
            (*PROJECT_TOPIC_KINDS, *params, *closed),
        ).fetchall()
    topics = [t for r in rows if (t := collect_node(db, r["id"])) is not None]
    return {
        "project_id": project_id,
        "name": (project or {}).get("name") or project_id,
        "objective": (project or {}).get("objective") or "",
        "topics": topics,
    }


def resolve_target(db, ident: str) -> tuple[str, str] | None:
    """Resolve a user-supplied id to ``("project", pid)`` or ``("node", id)``.

    Order (§7.1 disambiguation): a project id wins, then an exact node id, then
    a conversation's two-letter user label. Returns None when nothing matches —
    the caller decides the exit code.
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    if ident == INBOX_PROJECT_ID or db.get_project(ident) is not None:
        return ("project", ident)
    with db._connect() as conn:
        row = conn.execute("SELECT id FROM nodes WHERE id=?", (ident,)).fetchone()
    if row is not None:
        return ("node", row["id"])
    thread = db.get_thread_by_user_label(ident)
    if thread is not None:
        return ("node", thread["id"])
    return None
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
uv run pytest -q tests/test_notebook_collect.py
```
Expected: all PASS.

- [ ] **Step 6: Full suite + commit**

```bash
uv run pytest -q
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
git add src/juggle_notebook.py src/juggle_settings.py tests/test_notebook_collect.py
git commit -m "feat(notebook): graph collection, configurable path, atomic materialize

collect_node/collect_project turn nodes+node_edges+node_notes into the dict the
pure renderer consumes. Project view is ON-READ aggregation over open topics —
no materialized project file, nothing to invalidate. Node files are overwritten
wholesale through tmp+os.replace so concurrent regenerates converge.
Adds paths.notebooks_dir (default ~/.claude/juggle/notebooks) + the
JUGGLE_NOTEBOOKS_DIR override.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_notebook_collect.py
uv run python -c "
import sys; sys.path.insert(0,'src')
from juggle_settings import get_settings
print(get_settings()['paths']['notebooks_dir'])"      # absolute, expanded
# No task/edge write path exists in the notebook layer:
! grep -nE '\b(UPDATE nodes|INSERT INTO nodes|INSERT INTO node_edges|DELETE FROM)' src/juggle_notebook.py && echo NO-WRITE-PATH-OK
```

---

# Task 5: CLI — `juggle notebook show` / `juggle notebook append`

**Files:**
- Create: `src/juggle_cmd_notebook.py`
- Modify: `src/juggle_cli_commands_misc.py` (two `Cmd` entries)
- Modify: `tests/test_cli_verb_vocab.py` (add `append` to `CLOSED_VERBS`)
- Test: `tests/test_cmd_notebook.py`

**Interfaces:**
- Consumes: `juggle_notebook.{resolve_target, collect_node, collect_project, materialize}`, `juggle_notebook_render.{render_node, render_project}`, `dbops.node_notes.append_note`, `juggle_cli_common.get_db`.
- Produces:
  - `cmd_notebook_show(args) -> None` — reads `args.target`, `args.json_out`, `args.db_path`.
  - `cmd_notebook_append(args) -> None` — reads `args.node_id`, `args.body`, `args.who`, `args.db_path`.
  - `default_who() -> str` — `"agent"` in agent context, else `"orch"`.

**Contract:** `--json` for a node emits exactly spec §7.3 (`node_id, kind, state, project_id, title, context, tasks, log, markdown`); for a project, `{project_id, name, topics: [...], markdown}`. Unknown target → stderr message + `sys.exit(1)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_cmd_notebook.py`:

```python
"""`juggle notebook show|append` CLI (spec 2026-06-27 §7).

Agent-first: every assertion here is something an agent can check with no human
— a JSON schema, an exit code, a file's bytes, a row count.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_notebook as nb  # noqa: E402
from dbops import node_notes  # noqa: E402
from juggle_cmd_notebook import cmd_notebook_append, cmd_notebook_show  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402
from helpers.node_seed import seed_node  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "cli.db"))
    d.init_db()
    with d._connect() as conn:
        seed_node(conn, id="topic1", kind="topic", title="Add OAuth login",
                  state="open", objective="OAuth please.")
        seed_node(conn, id="t1", kind="task", title="Config", state="verified",
                  parent_id="topic1")
        seed_node(conn, id="t2", kind="task", title="Handler", state="open",
                  parent_id="topic1")
        conn.executemany(
            "INSERT INTO node_edges (node_id, depends_on_id, kind) VALUES (?,?,'dep')",
            [("t2", "t1")],
        )
        conn.commit()
    return d


@pytest.fixture(autouse=True)
def notebooks_tmp(tmp_path, monkeypatch):
    target = tmp_path / "notebooks"
    monkeypatch.setenv(nb.NOTEBOOKS_DIR_ENV, str(target))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return target


def _show(db, target, json_out=False):
    cmd_notebook_show(SimpleNamespace(target=target, json_out=json_out,
                                      db_path=str(db.db_path)))


def _append(db, node_id, body, who=None):
    cmd_notebook_append(SimpleNamespace(node_id=node_id, body=body, who=who,
                                        db_path=str(db.db_path)))


def test_show_node_prints_markdown(db, capsys):
    _show(db, "topic1")
    out = capsys.readouterr().out
    assert out.startswith("# Add OAuth login\n")
    assert "## Tasks" in out and "- [x] Config" in out


def test_show_node_json_matches_the_spec_shape(db, capsys):
    _append(db, "topic1", "kicked off")
    _show(db, "topic1", json_out=True)
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"node_id", "kind", "state", "project_id", "title",
                         "context", "tasks", "log", "markdown"}
    assert data["node_id"] == "topic1"
    assert set(data["context"]) == {"objective", "last_user_intent"}
    assert {"id", "title", "state", "glyph", "blocked_by"} <= set(data["tasks"][0])
    assert {"id", "ts", "who", "body"} <= set(data["log"][0])
    assert data["markdown"].startswith("# Add OAuth login")


def test_show_refreshes_the_materialized_file_to_match_stdout(db, capsys, notebooks_tmp):
    _show(db, "topic1")
    stdout = capsys.readouterr().out
    assert (notebooks_tmp / "topic1.md").read_text() == stdout


def test_show_regenerates_the_file_after_a_graph_change(db, notebooks_tmp):
    _show(db, "topic1")
    before = (notebooks_tmp / "topic1.md").read_text()
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='verified' WHERE id='t2'")
        conn.commit()
    _show(db, "topic1")
    after = (notebooks_tmp / "topic1.md").read_text()
    assert "- [ ] Handler" in before and "- [x] Handler" in after
    assert after.count("## Tasks") == 1        # regenerated, never appended


def test_show_unknown_target_exits_nonzero(db):
    with pytest.raises(SystemExit) as excinfo:
        _show(db, "no-such-id")
    assert excinfo.value.code != 0


def test_show_project_aggregates_open_topics_and_writes_no_project_file(
        db, capsys, notebooks_tmp):
    pid = db.create_project("Webapp", "Ship auth.")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET project_id=? WHERE id='topic1'", (pid,))
        conn.commit()
    _show(db, pid, json_out=True)
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"project_id", "name", "topics", "markdown"}
    assert [t["node_id"] for t in data["topics"]] == ["topic1"]
    assert not (notebooks_tmp / f"{pid}.md").exists()


def test_append_writes_one_row_and_shows_up_in_the_log(db, capsys):
    _append(db, "topic1", "first")
    _append(db, "topic1", "second")
    _show(db, "topic1", json_out=True)
    log = json.loads(capsys.readouterr().out)["log"]
    assert [n["body"] for n in log] == ["first", "second"]
    assert [n["id"] for n in log] == sorted(n["id"] for n in log)


def test_append_defaults_who_to_orch_outside_agent_context(db):
    _append(db, "topic1", "note")
    assert node_notes.list_notes(db, "topic1")[0]["who"] == "orch"


def test_append_defaults_who_to_agent_inside_agent_context(db, monkeypatch):
    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr("juggle_cmd_notebook.spool_event_if_agent", lambda *a, **k: False)
    _append(db, "topic1", "note")
    assert node_notes.list_notes(db, "topic1")[0]["who"] == "agent"


def test_append_explicit_who_wins(db):
    _append(db, "topic1", "note", who="agent")
    assert node_notes.list_notes(db, "topic1")[0]["who"] == "agent"


def test_append_rejects_empty_body(db):
    with pytest.raises(SystemExit) as excinfo:
        _append(db, "topic1", "   ")
    assert excinfo.value.code != 0
    assert node_notes.list_notes(db, "topic1") == []


def test_append_rejects_unknown_node(db):
    with pytest.raises(SystemExit) as excinfo:
        _append(db, "no-such-node", "note")
    assert excinfo.value.code != 0


def test_append_reads_body_from_stdin_when_dash(db, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    _append(db, "topic1", "-")
    assert node_notes.list_notes(db, "topic1")[0]["body"] == "from stdin"


# ── CLI wiring ────────────────────────────────────────────────────────────────

def test_notebook_commands_are_registered_on_the_live_parser():
    from juggle_cli import build_cli_parser

    parser = build_cli_parser(vault_path_default="/tmp")
    show = parser.parse_args(["notebook", "show", "topic1"])
    assert show.func.__name__ == "cmd_notebook_show"
    ap = parser.parse_args(["notebook", "append", "topic1", "hello", "--who", "agent"])
    assert ap.func.__name__ == "cmd_notebook_append"
    assert ap.node_id == "topic1" and ap.body == "hello" and ap.who == "agent"


def test_notebook_show_json_flag_parses():
    from juggle_cli import build_cli_parser

    parser = build_cli_parser(vault_path_default="/tmp")
    assert parser.parse_args(["notebook", "show", "P1", "--json"]).json_out is True


def test_no_notebook_task_mutation_verbs_exist():
    """REGRESSION PIN (spec §7.4): the notebook has NO task write path. A
    `notebook add-task`/`check`/`edit-task` verb would create a second writer
    that can drift from the graph — the exact failure DA5 rules out."""
    from juggle_cli_commands import COMMANDS

    verbs = {c.verb for c in COMMANDS if c.resource == "notebook"}
    assert verbs == {"show", "append"}
```

- [ ] **Step 2: Run it to confirm RED**

```bash
uv run pytest -q tests/test_cmd_notebook.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_cmd_notebook'`.

- [ ] **Step 3: Create `src/juggle_cmd_notebook.py`**

```python
"""juggle_cmd_notebook — `juggle notebook show|append` handlers (spec §7).

Owns: the two notebook CLI verbs. ``show`` is a pure read plus a refresh of the
generated file; ``append`` is the ONLY write the notebook CLI performs.
Must not own: the render format (juggle_notebook_render), collection/paths
(juggle_notebook), the notes store (dbops.node_notes).

There is deliberately NO add-task / check / edit-task verb (§7.4): sub-task
creation and state changes go through the existing graph ops, so the checkbox
list can never drift from the graph — there is no second write path.
"""
from __future__ import annotations

import json
import sys

from juggle_spool_cli_common import should_spool, spool_event_if_agent

# The two conventional `who` values (§7.2). Free text in v1.
WHO_AGENT = "agent"
WHO_ORCH = "orch"


def default_who() -> str:
    """`--who` default, derived from caller context: a dispatched agent writes
    as 'agent', the orchestrator/live session as 'orch'. Reuses the ONE existing
    agent-context detector — never a second heuristic."""
    return WHO_AGENT if should_spool() else WHO_ORCH


def _read_body(raw) -> str:
    """The note body from an argument, or stdin when it is '-' or omitted."""
    if raw is None or raw == "-":
        return sys.stdin.read().strip()
    return str(raw).strip()


def cmd_notebook_show(args) -> None:
    """`juggle notebook show <node_id|project_id|label> [--json]`."""
    from juggle_cli_common import get_db
    import juggle_notebook as nbk
    from juggle_notebook_render import render_node, render_project

    db = get_db(getattr(args, "db_path", None), init=False)
    target = nbk.resolve_target(db, getattr(args, "target", ""))
    if target is None:
        print(f"Error: {getattr(args, 'target', '')!r} is neither a project nor a node.",
              file=sys.stderr)
        sys.exit(1)

    kind, ident = target
    if kind == "project":
        data = nbk.collect_project(db, ident)
        markdown = render_project(data)
        # On-read aggregation (§8): no project file is ever materialized, but
        # each open topic's own file is refreshed so an agent reading a path
        # sees current state.
        for topic in data["topics"]:
            nbk.materialize(topic["node_id"], render_node(topic))
        payload = {"project_id": data["project_id"], "name": data["name"],
                   "topics": data["topics"], "markdown": markdown}
    else:
        data = nbk.collect_node(db, ident)
        markdown = render_node(data)
        nbk.materialize(ident, markdown)
        payload = {**data, "markdown": markdown}

    if getattr(args, "json_out", False):
        print(json.dumps(payload, ensure_ascii=False))
        return
    print(markdown, end="")


def cmd_notebook_append(args) -> None:
    """`juggle notebook append <node_id> "<note>" [--who agent|orch]`.

    Append-only: two identical notes yield two rows (never deduped), and there
    is no edit or delete in v1.
    """
    body = _read_body(getattr(args, "body", None))
    who = getattr(args, "who", None) or default_who()

    # Validate BEFORE any spool/DB touch so an agent gets immediate fail-loud
    # feedback instead of a deferred dead-letter at replay time (mirrors
    # cmd_graph_learn).
    if not body:
        print("Error: note body is empty — nothing to append.", file=sys.stderr)
        sys.exit(1)

    node_id = getattr(args, "node_id", None)
    # Agent context: spool the write for the single-writer watchdog broker —
    # agent processes must not open the shared DB read-write.
    if spool_event_if_agent("notebook_append",
                            {"node_id": node_id, "body": body, "who": who}):
        print(f"notebook note for {node_id} → spooled")
        return

    from dbops import node_notes
    from juggle_cli_common import get_db

    db = get_db(getattr(args, "db_path", None), init=True)
    with db._connect() as conn:
        exists = conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone()
    if exists is None:
        print(f"Error: node {node_id!r} not found.", file=sys.stderr)
        sys.exit(1)

    note_id = node_notes.append_note(db, node_id, body, who=who)
    print(f"notebook note {note_id} appended to {node_id} (who={who}).")
```

- [ ] **Step 4: Register the commands in `src/juggle_cli_commands_misc.py`**

Add the import next to the other handler imports:

```python
from juggle_cmd_notebook import cmd_notebook_append, cmd_notebook_show
```

Add these two entries inside the `MISC_COMMANDS` tuple:

```python
    Cmd("notebook", "show", cmd_notebook_show,
        args=(
            Arg("target", help="Node id, project id (or INBOX), or a topic label"),
            Arg("--json", dest="json_out", action="store_true",
                help="Emit the structured notebook object (spec §7.3)"),
            Arg("--db", dest="db_path", default=None, help="Path to juggle.db"),
        ),
        help="Render a node's notebook, or a project's aggregate of open topics"),
    Cmd("notebook", "append", cmd_notebook_append,
        args=(
            Arg("node_id", help="Node id whose Log gains the note"),
            Arg("body", nargs="?", default=None,
                help="Note text (omit or pass '-' to read from stdin)"),
            Arg("--who", default=None,
                help="Author tag: agent|orch (default: derived from caller context)"),
            Arg("--db", dest="db_path", default=None, help="Path to juggle.db"),
        ),
        help="Append one narrative note to a node's notebook Log (append-only)"),
```

- [ ] **Step 5: Extend the closed verb vocabulary**

`tests/test_cli_verb_vocab.py` enforces a CLOSED verb set whose own docstring
states "additions require updating the lint allowlist". `append` is a new
sanctioned action, so add it to `CLOSED_VERBS` (this is the documented
extension mechanism — it is **not** a weakening of the pin, and
`test_lint_has_teeth_rejects_novel_verb` still proves the lint bites):

```python
    "retain", "grep", "next",
    # notebook append (2026-07-25, spec 2026-06-27 §7.2): the notebook Log is
    # append-only by contract — no other closed verb ("create"/"update"/"set")
    # names that semantics without implying an editable record.
    "append",
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
uv run pytest -q tests/test_cmd_notebook.py tests/test_cli_verb_vocab.py \
                tests/test_cli_spec_port.py tests/test_cli_main_parser.py
```
Expected: all PASS.

- [ ] **Step 7: Drive the real CLI end to end**

```bash
export JUGGLE_DB_PATH=/tmp/nb-cli.db JUGGLE_NOTEBOOKS_DIR=/tmp/nb-books
rm -f "$JUGGLE_DB_PATH"; rm -rf "$JUGGLE_NOTEBOOKS_DIR"
uv run src/juggle_cli.py db init
TID=$(uv run src/juggle_cli.py thread create "Notebook smoke" | head -1)
uv run src/juggle_cli.py notebook --help
uv run src/juggle_cli.py notebook show INBOX --json | head -c 400; echo
unset JUGGLE_DB_PATH JUGGLE_NOTEBOOKS_DIR
```
Expected: `notebook --help` lists `show` and `append`; the INBOX JSON parses and
contains a `topics` array.

- [ ] **Step 8: Full suite + commit**

```bash
uv run pytest -q
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
git add src/juggle_cmd_notebook.py src/juggle_cli_commands_misc.py \
        tests/test_cmd_notebook.py tests/test_cli_verb_vocab.py
git commit -m "feat(notebook): juggle notebook show|append CLI

show renders a node or a project aggregate (--json emits the spec §7.3 shape)
and refreshes the generated file; append is the ONLY notebook write. No
add-task/check verb exists by design — task state lives on the graph and has
exactly one writer (spec §7.4). Adds 'append' to the closed CLI verb vocabulary
via its documented extension mechanism.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_cmd_notebook.py
uv run src/juggle_cli.py notebook --help | grep -q "append" && echo VERB-OK
uv run python -c "
import sys; sys.path.insert(0,'src')
from juggle_cli_commands import COMMANDS
print(sorted(c.verb for c in COMMANDS if c.resource=='notebook'))"   # ['append', 'show']
```

---

# Task 6: Spool routing — `notebook append` from a dispatched agent

A dispatched agent runs in a worktree and must **not** open the shared DB
read-write; its writes go through the spool, drained by the single-writer
watchdog. Task 5 already writes the spool event; this task makes the drain side
route it, and pins the shape contract.

**Files:**
- Modify: `src/juggle_spool_apply.py` (`_NS_DEFAULTS`, `_dispatch`)
- Modify: `tests/test_spool_apply_event_shape.py` (`WRITER_ARG_KEYS`)
- Modify: `tests/test_agent_context_write_audit.py` (add the new handler)
- Test: `tests/test_cmd_notebook.py` (extend)

**Interfaces:**
- Consumes: `juggle_cmd_notebook.cmd_notebook_append`.
- Produces: spool event type `"notebook_append"` with args `{node_id, body, who}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cmd_notebook.py`:

```python
# ── spool routing (single-writer broker) ──────────────────────────────────────

def test_append_spools_instead_of_writing_in_agent_context(db, monkeypatch):
    """An agent process must never open the shared DB read-write — the note is
    spooled and applied by the watchdog drain."""
    written = []
    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr("dbops.spool.write_event",
                        lambda d, t, a, b, args: written.append((t, args)) or "uuid")
    _append(db, "topic1", "from the agent", who="agent")
    assert written == [("notebook_append",
                        {"node_id": "topic1", "body": "from the agent", "who": "agent"})]
    assert node_notes.list_notes(db, "topic1") == []


def test_spooled_notebook_append_applies_on_drain(db, tmp_path, monkeypatch):
    """REGRESSION SHAPE PIN: the replayed Namespace must expose every key the
    writer emits (2026-07-02 class: a missing _NS_DEFAULTS key dead-lettered a
    real event with AttributeError before its own validation could run)."""
    from dbops.spool import write_event
    from juggle_spool_apply import apply_event
    from dbops.spool import read_pending

    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr("juggle_spool_apply.spool_dir", lambda: spool)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    monkeypatch.setenv("JUGGLE_DB_PATH", str(db.db_path))

    write_event(spool, "notebook_append", "", "",
                {"node_id": "topic1", "body": "replayed", "who": "agent"})
    for event in read_pending(spool):
        ok, _msg = apply_event(db, event)
        assert ok, _msg
    assert [n["body"] for n in node_notes.list_notes(db, "topic1")] == ["replayed"]
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest -q tests/test_cmd_notebook.py -k spool
```
Expected: FAIL — `ValueError: unknown spool event type 'notebook_append'`.

- [ ] **Step 3: Route the event in `src/juggle_spool_apply.py`**

Extend `_NS_DEFAULTS` (add the two new keys; `node_id` is already present):

```python
    type="manual_step", priority="normal", fail=False, db_path=None,
    body=None, who=None,
```

Add the branch in `_dispatch`, just before the final `else`:

```python
    elif event.type == "notebook_append":
        from juggle_cmd_notebook import cmd_notebook_append
        cmd_notebook_append(_ns(event))
```

- [ ] **Step 4: Extend the two shared contract pins**

`tests/test_spool_apply_event_shape.py` — add to `WRITER_ARG_KEYS`:

```python
    # cmd_notebook_append (juggle_cmd_notebook.py)
    "notebook_append": {"node_id", "body", "who"},
```

`tests/test_agent_context_write_audit.py` — add to the parametrize list:

```python
    ("juggle_cmd_notebook", "cmd_notebook_append"),
```

and extend that test's `argparse.Namespace(...)` with the keys the new handler
reads:

```python
        body="a note", who="agent", node_id="topic1",
```

- [ ] **Step 5: Verify**

```bash
uv run pytest -q tests/test_cmd_notebook.py tests/test_spool_apply_event_shape.py \
                tests/test_agent_context_write_audit.py
uv run pytest -q
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/juggle_spool_apply.py tests/test_cmd_notebook.py \
        tests/test_spool_apply_event_shape.py tests/test_agent_context_write_audit.py
git commit -m "feat(notebook): route notebook_append through the spool broker

A dispatched agent never opens the shared DB read-write: notebook append spools
and the watchdog drain applies it through the SAME handler (no parallel
reimplementation). Adds the event to both shared contract pins.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_spool_apply_event_shape.py tests/test_agent_context_write_audit.py
uv run python -c "
import sys; sys.path.insert(0,'src')
import juggle_spool_apply as s
print('notebook_append' in open('src/juggle_spool_apply.py').read())"   # True
```

---

# Task 7: Lifecycle hook — `send-task` injects the notebook path + protocol

Enforcement lives in **code**, never a prompt: every dispatched agent
deterministically receives where its notebook is and how to keep it current.

**Files:**
- Create: `src/juggle_notebook_hooks.py`
- Modify: `src/juggle_dispatch_core.py` (`send_task_to_agent`)
- Test: `tests/test_notebook_hooks.py`

**Interfaces:**
- Consumes: `dbops.db_topics.get_topic_by_thread`, `juggle_notebook.{collect_node, materialize, notebook_path}`, `juggle_notebook_render.render_node`, `juggle_dispatch_literal._cli_invocation_prefix`.
- Produces:
  - `build_notebook_section(node_id: str, path: str, cli_path: str) -> str` — pure.
  - `notebook_node_for_thread(db, thread_id: str | None) -> str | None` — the bound topic node id, else the conversation node id, else None.
  - `notebook_section_for_thread(db, thread_id: str | None) -> str` — best-effort; `""` on any failure.

- [ ] **Step 1: Write the failing test**

Create `tests/test_notebook_hooks.py`:

```python
"""Notebook lifecycle hooks (spec 2026-06-27 §9) — code-enforced, not prompt.

§9.1 send-task: the dispatched prompt carries the node's notebook PATH and the
update PROTOCOL, and the file is materialized, without anyone remembering to
say so.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import juggle_notebook as nb  # noqa: E402
import juggle_notebook_hooks as hooks  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402
from helpers.node_seed import seed_node  # noqa: E402


@pytest.fixture(autouse=True)
def notebooks_tmp(tmp_path, monkeypatch):
    target = tmp_path / "notebooks"
    monkeypatch.setenv(nb.NOTEBOOKS_DIR_ENV, str(target))
    return target


# ── pure section builder ──────────────────────────────────────────────────────

def test_build_notebook_section_names_the_path_and_the_protocol():
    section = hooks.build_notebook_section("abc123", "/tmp/books/abc123.md", "juggle")
    assert "/tmp/books/abc123.md" in section
    assert "juggle notebook append abc123" in section
    assert "do NOT hand-edit" in section
    assert section.endswith("\n\n---\n\n")


def test_build_notebook_section_points_task_state_at_the_graph():
    """The protocol must send task-state changes to graph ops, never to the
    notebook — the notebook has no task write path (§7.4)."""
    section = hooks.build_notebook_section("abc", "/p.md", "juggle")
    assert "graph" in section
    assert "notebook add-task" not in section


# ── node resolution ───────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "hooks.db"))
    d.init_db()
    return d


def test_notebook_node_for_thread_prefers_the_bound_topic(db):
    tid = db.create_thread("Chat", session_id="s")
    with db._connect() as conn:
        seed_node(conn, id="topicX", kind="topic", title="Bound topic")
        conn.execute(
            "INSERT INTO node_edges (node_id, depends_on_id, kind) "
            "VALUES ('topicX', ?, 'dispatch')", (tid,))
        conn.commit()
    assert hooks.notebook_node_for_thread(db, tid) == "topicX"


def test_notebook_node_for_thread_falls_back_to_the_conversation(db):
    tid = db.create_thread("Bare chat", session_id="s")
    assert hooks.notebook_node_for_thread(db, tid) == tid


def test_notebook_node_for_thread_handles_no_thread(db):
    assert hooks.notebook_node_for_thread(db, None) is None


def test_notebook_section_for_thread_is_best_effort(db):
    """Never breaks dispatch (matches the ledger-write convention)."""
    assert hooks.notebook_section_for_thread(db, "does-not-exist") == ""


# ── §9.1 the REAL dispatch path ───────────────────────────────────────────────

def _dispatch(tmp_path, monkeypatch, role="coder"):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=repo, check=True)

    monkeypatch.setenv("JUGGLE_WORKTREE_ROOT", str(tmp_path / "wts"))
    (tmp_path / "wts").mkdir()
    import juggle_dispatch_core as _core
    monkeypatch.setattr(_core, "DEFAULT_WORKTREE_ROOT", str(tmp_path / "wts"))

    db = JuggleDB(db_path=str(tmp_path / "d.db"))
    db.init_db()
    db.set_active(True)
    thread_id = db.create_thread("t1", session_id="")
    agent_id = db.create_agent(role, "%fake", repo_path=str(repo))

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.send_task.return_value = "mock_hash"
    mgr.run_task_oneshot.return_value = ("mock_hash", None)

    _core.send_task_to_agent(db, agent_id, thread_id, "Do the thing.", _mgr=mgr)
    return db, thread_id, db.get_agent(agent_id)["last_task"]


def test_dispatched_prompt_carries_the_notebook_path_and_protocol(tmp_path, monkeypatch):
    db, thread_id, prompt = _dispatch(tmp_path, monkeypatch)
    assert str(nb.notebook_path(thread_id)) in prompt
    assert f"juggle notebook append {thread_id}" in prompt


def test_dispatch_materializes_the_notebook_file(tmp_path, monkeypatch, notebooks_tmp):
    db, thread_id, _prompt = _dispatch(tmp_path, monkeypatch)
    assert (notebooks_tmp / f"{thread_id}.md").is_file()


def test_notebook_section_appears_exactly_once(tmp_path, monkeypatch):
    _db, _tid, prompt = _dispatch(tmp_path, monkeypatch)
    assert prompt.count("## Notebook") == 1


def test_dispatch_survives_a_broken_notebook_layer(tmp_path, monkeypatch):
    """REGRESSION PIN: a notebook failure must never break a dispatch — the
    section is best-effort, exactly like the ledger write."""
    import juggle_notebook_hooks as h

    def _boom(*a, **k):
        raise RuntimeError("notebook exploded")

    monkeypatch.setattr(h, "notebook_node_for_thread", _boom)
    _db, _tid, prompt = _dispatch(tmp_path, monkeypatch)
    assert "Do the thing." in prompt
    assert "## Notebook" not in prompt
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest -q tests/test_notebook_hooks.py
```
Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_notebook_hooks'`.

- [ ] **Step 3: Create `src/juggle_notebook_hooks.py`**

```python
"""juggle_notebook_hooks — notebook behaviour at the two lifecycle points
(spec 2026-06-27 §9). Enforcement is CODE, never prompt text: a prompt can be
forgotten, a hook cannot.

Owns: the dispatch-time '## Notebook' section (§9.1) and the completion-time Log
append + left-behind-WIP warning (§9.2).
Must not own: prompt assembly (juggle_dispatch_core), the completion state
machine (juggle_cmd_agents_complete), the render (juggle_notebook_render).

BEST-EFFORT BY CONTRACT: every entry point here swallows its own exceptions and
degrades to a no-op. A notebook problem must never break a dispatch or a
completion — the same convention the ledger write in dispatch_core follows.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_PROTOCOL = (
    "Your sub-task state IS the graph — change it with `juggle graph` ops "
    "(mark-task / add-task), never by editing the notebook. Record narrative "
    "(what you tried, what failed and why, what is next) with "
    "`{cli} notebook append {node_id} \"…\"`. The notebook file is GENERATED "
    "from the graph on every render: do NOT hand-edit it."
)


def build_notebook_section(node_id: str, path: str, cli_path: str) -> str:
    """PURE: the '## Notebook' dispatch-prompt section for ``node_id``."""
    return (
        "## Notebook\n"
        f"Your working notebook: {path}\n"
        + _PROTOCOL.format(cli=cli_path, node_id=node_id)
        + "\n\n---\n\n"
    )


def notebook_node_for_thread(db, thread_id: str | None) -> str | None:
    """The node whose notebook this dispatch belongs to.

    A thread bound to a graph topic notebooks the TOPIC (that is where the
    sub-tasks hang); a bare conversation notebooks itself. Post-P8 the node
    always already exists — this resolves it, it never creates one, so there is
    no notebook setup ceremony (§9.1 step 1).
    """
    if not thread_id:
        return None
    from dbops import db_topics

    topic = db_topics.get_topic_by_thread(db, thread_id)
    if topic:
        return topic["id"]
    return thread_id if db.get_thread(thread_id) else None


def notebook_section_for_thread(db, thread_id: str | None) -> str:
    """DB wrapper: materialize the notebook and return its prompt section, or ""
    when there is no resolvable node (or anything at all goes wrong)."""
    try:
        node_id = notebook_node_for_thread(db, thread_id)
        if not node_id:
            return ""
        import juggle_notebook as nbk
        from juggle_dispatch_literal import _cli_invocation_prefix
        from juggle_notebook_render import render_node

        data = nbk.collect_node(db, node_id)
        if data is None:
            return ""
        path = nbk.materialize(node_id, render_node(data))
        return build_notebook_section(node_id, str(path), _cli_invocation_prefix())
    except Exception as e:  # best-effort: never break a dispatch
        _log.warning("notebook section skipped for thread %s: %s", thread_id, e)
        return ""
```

- [ ] **Step 4: Wire it into `send_task_to_agent`**

In `src/juggle_dispatch_core.py`, immediately after the existing
`_source_of_truth` assignment, add:

```python
    # '## Notebook' section (spec 2026-06-27 §9.1): every dispatched agent gets
    # its notebook PATH + update PROTOCOL deterministically, in code — never by
    # the orchestrator remembering to mention it. Best-effort: "" on any failure.
    from juggle_notebook_hooks import notebook_section_for_thread
    _notebook = notebook_section_for_thread(db, thread_id)
```

Then add `_notebook` to **both** prompt-composition branches, right after
`_source_of_truth`:

```python
        full_prompt = (
            _worktree_context + _source_of_truth + _notebook
            + render_agent_dispatch_prompt(
                prompt, role=_role, thread_wt=thread_wt, agent=agent,
                thread_label=thread_label,
            )
        )
```

```python
        full_prompt = (
            _com.UNIVERSAL_PREAMBLE + _worktree_context + _source_of_truth
            + _notebook + prompt.rstrip()
        )
```

- [ ] **Step 5: Verify**

```bash
uv run pytest -q tests/test_notebook_hooks.py tests/test_dispatch_prompts.py \
                tests/test_dispatch_core.py tests/test_send_task_forwardlink.py
uv run pytest -q
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
```
Expected: all PASS, `LOC-OK`. If a `tests/test_dispatch_prompts.py` count
assertion trips (`count(...) == 1`), the notebook section is repeating a phrase
those pins own — reword `_PROTOCOL`, do **not** weaken the pin.

- [ ] **Step 6: Commit**

```bash
git add src/juggle_notebook_hooks.py src/juggle_dispatch_core.py tests/test_notebook_hooks.py
git commit -m "feat(notebook): send-task injects the notebook path + protocol

Code-enforced, not prompt-enforced (spec §9.1): every dispatch resolves the
node, materializes its notebook, and injects the path plus the update protocol
(graph ops for task state, notebook append for narrative, never hand-edit).
Best-effort — a notebook failure can never break a dispatch.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_notebook_hooks.py
# The ledger proves it end to end (spec §12): after a real dispatch the recorded
# input_prompt contains the notebook path.
uv run pytest -q tests/test_notebook_hooks.py -k "path_and_protocol or materializes"
```

---

# Task 8: Lifecycle hook — `agent complete` appends a Log entry and warns on WIP

**Files:**
- Modify: `src/juggle_notebook_hooks.py` (add `record_completion`)
- Modify: `src/juggle_cmd_agents_complete.py` (call it)
- Test: `tests/test_notebook_hooks.py` (extend)

**Interfaces:**
- Consumes: `dbops.node_notes.append_note`, `juggle_notebook.collect_node`, `db.add_action_item_once`.
- Produces: `record_completion(db, thread_id: str, result_summary: str) -> None` — best-effort.

**Placement rationale (deterministic, and it matters):** the call goes
**after** the `enforce_handoff_contract` / `enforce_topic_gate` refusal gates
(so a refused completion writes nothing) and **before**
`finalize_or_detach_integrate` (so the WIP check reads the child states as the
agent left them, not as integrate rewrote them).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_notebook_hooks.py`:

```python
# ── §9.2 completion hook ──────────────────────────────────────────────────────

import argparse  # noqa: E402

from dbops import node_notes  # noqa: E402


def _seed_children(db, parent, states):
    with db._connect() as conn:
        for i, state in enumerate(states):
            seed_node(conn, id=f"{parent}-c{i}", kind="task", title=f"Task {i}",
                      state=state, parent_id=parent)
        conn.commit()


def test_record_completion_appends_a_log_entry(db):
    tid = db.create_thread("Topic", session_id="s")
    hooks.record_completion(db, tid, "Landed the thing.")
    log = node_notes.list_notes(db, tid)
    assert len(log) == 1
    assert "Landed the thing." in log[0]["body"]
    assert log[0]["who"] == "agent"


def test_record_completion_warns_when_wip_left_behind(db):
    """Children in progress and NONE verified => the agent claimed completion
    while leaving work in flight. A signal, not a refusal (§9.2)."""
    tid = db.create_thread("Topic", session_id="s")
    _seed_children(db, tid, ["running", "open"])
    hooks.record_completion(db, tid, "Done?")
    messages = [i["message"] for i in db.get_open_action_items()]
    assert any("left in progress" in m for m in messages), messages


def test_record_completion_does_not_warn_when_something_verified(db):
    tid = db.create_thread("Topic", session_id="s")
    _seed_children(db, tid, ["running", "verified"])
    hooks.record_completion(db, tid, "Partial.")
    assert not any("left in progress" in i["message"] for i in db.get_open_action_items())


def test_record_completion_does_not_warn_with_no_children(db):
    tid = db.create_thread("Topic", session_id="s")
    hooks.record_completion(db, tid, "Ad-hoc done.")
    assert not any("left in progress" in i["message"] for i in db.get_open_action_items())


def test_record_completion_warning_is_filed_once_per_signature(db):
    tid = db.create_thread("Topic", session_id="s")
    _seed_children(db, tid, ["running"])
    hooks.record_completion(db, tid, "Done?")
    hooks.record_completion(db, tid, "Done?")
    warns = [i for i in db.get_open_action_items() if "left in progress" in i["message"]]
    assert len(warns) == 1


def test_record_completion_is_best_effort(db, monkeypatch):
    """REGRESSION PIN: a notebook failure must never break `agent complete`."""
    monkeypatch.setattr("dbops.node_notes.append_note",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    hooks.record_completion(db, "whatever", "summary")   # must not raise


def test_complete_agent_appends_to_the_notebook_log(db, tmp_path, monkeypatch):
    """The REAL `juggle agent complete` path writes the Log entry."""
    from juggle_cmd_agents_complete import cmd_complete_agent

    monkeypatch.setenv("JUGGLE_DB_PATH", str(db.db_path))
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    tid = db.create_thread("Completion topic", session_id="s")
    db.update_thread(tid, status="background")
    cmd_complete_agent(argparse.Namespace(
        thread_id=tid, result_summary="Shipped it.", retain_text=None,
        role="coder", open_questions=None, handoff=None,
    ))
    assert any("Shipped it." in n["body"] for n in node_notes.list_notes(db, tid))
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest -q tests/test_notebook_hooks.py -k "completion or complete_agent"
```
Expected: FAIL — `AttributeError: module 'juggle_notebook_hooks' has no attribute 'record_completion'`.

- [ ] **Step 3: Add `record_completion` to `src/juggle_notebook_hooks.py`**

```python
# WIP warning wording — the substring the regression pin greps for. Changing it
# is a behaviour change, not a cosmetic one.
_WIP_WARNING = (
    "⚠️ Notebook: agent completed with sub-tasks left in progress and none "
    "verified — check {node_id} before treating this as done."
)


def record_completion(db, thread_id: str, result_summary: str) -> None:
    """§9.2: append the completion to the node's Log, and warn when the agent
    claimed completion while leaving work in flight.

    "In flight" = at least one child task rendered ``[/]`` and NOT ONE rendered
    ``[x]``. A signal (action item), never a refusal, in v1.

    Best-effort: any failure is logged and swallowed — a notebook problem must
    not fail a completion that already passed its real gates.
    """
    try:
        node_id = notebook_node_for_thread(db, thread_id)
        if not node_id:
            return
        import juggle_notebook as nbk
        from dbops import node_notes
        from juggle_notebook_render import GLYPH_ACTIVE, GLYPH_DONE

        node_notes.append_note(
            db, node_id, f"Agent completed: {result_summary}", who="agent")

        data = nbk.collect_node(db, node_id)
        glyphs = {t["glyph"] for t in (data or {}).get("tasks", [])}
        if GLYPH_ACTIVE in glyphs and GLYPH_DONE not in glyphs:
            db.add_action_item_once(
                thread_id=thread_id,
                message=_WIP_WARNING.format(node_id=node_id),
                type_="review",
                priority="normal",
            )
    except Exception as e:  # best-effort: never break a completion
        _log.warning("notebook completion record skipped for %s: %s", thread_id, e)
```

- [ ] **Step 4: Call it from `cmd_complete_agent`**

In `src/juggle_cmd_agents_complete.py`, immediately after the
`enforce_topic_gate(db, thread_uuid)` line and **before** the
`items_to_dismiss` snapshot, add:

```python
    # Notebook completion hook (spec §9.2). Placed AFTER the refusal gates (a
    # refused completion writes nothing) and BEFORE finalize/integrate (so the
    # WIP check sees the child states the AGENT left, not the ones integrate
    # rewrote). Best-effort — never breaks a completion.
    from juggle_notebook_hooks import record_completion
    record_completion(db, thread_uuid, args.result_summary)
```

Note: the item this may file is created *before* the `items_to_dismiss`
snapshot, so it is **not** swept into the auto-dismiss list — same reasoning as
the 2026-07-03 integrate-wedge Fix 4 comment already in that function.

- [ ] **Step 5: Verify**

```bash
uv run pytest -q tests/test_notebook_hooks.py tests/test_auto_action_items.py \
                tests/test_complete_agent_wedge_fixes.py \
                tests/test_spool_agent_complete_fail_writes.py \
                tests/test_completion_commands.py
uv run pytest -q
uv run python scripts/loc_gate.py >/dev/null && echo LOC-OK
```
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/juggle_notebook_hooks.py src/juggle_cmd_agents_complete.py tests/test_notebook_hooks.py
git commit -m "feat(notebook): agent complete appends a Log entry and warns on left-behind WIP

Spec §9.2, code-enforced. The Log entry and the WIP check run after the refusal
gates and before integrate, so the check reads the child states the agent left.
The warning is a signal (action item, deduped by signature), not a refusal.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
uv run pytest -q tests/test_notebook_hooks.py
# End to end against a real DB and the real CLI:
export JUGGLE_DB_PATH=/tmp/nb-e2e.db JUGGLE_NOTEBOOKS_DIR=/tmp/nb-e2e-books
rm -f "$JUGGLE_DB_PATH"; rm -rf "$JUGGLE_NOTEBOOKS_DIR"
uv run src/juggle_cli.py db init
uv run src/juggle_cli.py thread create "E2E notebook"
# (take the printed topic label as LBL)
# uv run src/juggle_cli.py notebook append <LBL> "checkpoint"
# uv run src/juggle_cli.py notebook show <LBL> --json | grep -q checkpoint && echo E2E-OK
unset JUGGLE_DB_PATH JUGGLE_NOTEBOOKS_DIR
```

---

# Task 9: Docs — unblock the spec, record the work

**Files:**
- Modify: `specs/2026-06-27-topic-project-notebooks.md` (status line + §4)
- Modify: `TODO.md`
- Modify: `docs/ARCHITECTURE.md` (code-map entries for the new modules)

- [ ] **Step 1: Update the spec status**

Replace the header line:

```markdown
_2026-06-27 · status: DESIGN (approved in brainstorming) · WHAT & WHY, not HOW_
```

with:

```markdown
_2026-06-27 · status: IMPLEMENTED 2026-07-25 (plan: `plan/2026-07-25-topic-project-notebooks.md`) · WHAT & WHY, not HOW_
```

And replace the blockquote precondition line:

```markdown
> **Precondition:** P8 collapse fully landed (see §4). This feature is **BLOCKED-ON** P8.
```

with:

```markdown
> **Precondition:** P8 collapse fully landed (see §4) — **SATISFIED 2026-07-25**
> (`juggle doctor --pre-p8-check --json` => `static.fail 0`, `import_refs 0`,
> runtime already-dropped, `pass true`). The legacy tables are DROPPED; this
> feature is built directly on `nodes` / `node_edges` with no dual-read.
```

Add a one-line note at the top of §4:

```markdown
> **STATUS 2026-07-25: this precondition is MET.** §4 is retained as the record
> of why the sequencing mattered; it is no longer a blocker.
```

- [ ] **Step 2: Update `TODO.md`**

Move the notebooks entry into the Done section as:

```markdown
- [x] Topic & Project Notebooks — node_notes store, pure render, `juggle notebook show|append`, dispatch + completion hooks ✅ 2026-07-25
```

- [ ] **Step 3: Update `docs/ARCHITECTURE.md`**

Add the new modules to the code map, in the style already used there:

```markdown
- `dbops/schema_notes.py` / `dbops/migration_76_node_notes.py` / `dbops/node_notes.py` —
  `node_notes`, the notebook's append-only narrative Log (the ONE new store).
- `juggle_notebook_render.py` — PURE node-subtree → markdown (Context/Tasks/Log).
- `juggle_notebook.py` — DB collection, notebook path config, atomic materialize.
- `juggle_cmd_notebook.py` — `juggle notebook show|append`.
- `juggle_notebook_hooks.py` — send-task prompt section + agent-complete Log/WIP hook.
```

- [ ] **Step 4: Verify and commit**

```bash
uv run pytest -q
graphify update .
git add specs/2026-06-27-topic-project-notebooks.md TODO.md docs/ARCHITECTURE.md
git commit -m "docs(notebook): mark the notebooks spec implemented; record the code map

P8 precondition verified met (doctor --pre-p8-check pass true), so the spec no
longer tells future agents the feature is blocked.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

**Agent-verifiable acceptance gate:**
```bash
grep -q "SATISFIED 2026-07-25" specs/2026-06-27-topic-project-notebooks.md && echo SPEC-OK
! grep -q "is \*\*BLOCKED-ON\*\* P8" specs/2026-06-27-topic-project-notebooks.md && echo UNBLOCKED-OK
```

---

# Final acceptance — the spec's own §12 table, mechanically

Run this after Task 9. Every row is spec §12's agent-verifiable criterion,
mapped to the test that proves it.

| Spec §12 component | Proof |
|---|---|
| Render = pure function | `tests/test_notebook_render.py::test_render_is_deterministic_byte_identical_twice` + `::test_render_node_matches_the_spec_contract_example` |
| Glyph derivation | `tests/test_notebook_render.py::test_glyph_for_each_state` (17 cases) + `tests/test_notebook_collect.py::test_collect_node_glyphs_and_blocked_by_match_the_graph` |
| `node_notes` append + order | `tests/test_node_notes.py` (7 tests, incl. no-dedup and same-second ordering) |
| CLI `show --json` shape | `tests/test_cmd_notebook.py::test_show_node_json_matches_the_spec_shape` |
| Project aggregation (on-read) | `tests/test_notebook_collect.py::test_collect_project_includes_open_topics_and_excludes_finished_ones` + `::test_collect_project_writes_no_materialized_project_file` |
| Materialized file | `tests/test_cmd_notebook.py::test_show_refreshes_the_materialized_file_to_match_stdout` + `::test_show_regenerates_the_file_after_a_graph_change` |
| `send-task` hook | `tests/test_notebook_hooks.py::test_dispatched_prompt_carries_the_notebook_path_and_protocol` + `::test_dispatch_materializes_the_notebook_file` |
| `complete-agent` hook | `tests/test_notebook_hooks.py::test_complete_agent_appends_to_the_notebook_log` + `::test_record_completion_warns_when_wip_left_behind` |

```bash
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle" JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q                                            # FULL suite green
uv run python scripts/loc_gate.py                           # no offenders
JUGGLE_DB_PATH=/tmp/nb-final.db uv run src/juggle_cli.py doctor --dry-run
```

Cockpit was not touched, so the viewport matrix (`cockpit --smoke
--all-viewports`) is not required — run it anyway if any cockpit file changed.

---

# Devil's Advocate

Self-critique of this plan, folded back in. Format: weakest assumption → failure
mode → mitigation (already applied unless marked RESIDUAL).

### DA-1 — "Every step keeps the suite green standalone" is the assumption most likely to be false

**Failure mode:** Tasks 5 and 6 look separable but are not, naively. Task 5's
`cmd_notebook_append` calls `spool_event_if_agent("notebook_append", …)`. Task 6
adds the drain-side route. If an agent-context append happens between the two
commits, the event lands in the spool and dead-letters with `unknown spool event
type`.

**Mitigation (applied):** Task 5's own tests never enter agent context (the
`notebooks_tmp` fixture pins `JUGGLE_ORCHESTRATOR=1`), so the suite is green at
Task 5's commit. The *runtime* gap is real but bounded — it exists only on a
branch, never on main, because the branch lands as one unit.
**RESIDUAL:** if a coder ships Task 5 to main alone, an agent's note dead-letters
until Task 6 lands. Acceptable: the dead-letter is loud, files an action item,
and loses nothing but a narrative line. Do not split these across PRs.

### DA-2 — The LOC gate is the most likely surprise failure

**Failure mode:** `juggle_dispatch_core.py` (296) and
`juggle_cmd_agents_complete.py` (300) are both at or under the limit by less
than the size of the hook they must receive. A coder who skips Tasks 1a/1b hits
`loc_gate.py` exit 1 **at the end** of Task 7 or 8 and either bloats the
allowlist (forbidden — it may only shrink) or unwinds the work.

**Mitigation (applied):** Tasks 1a and 1b exist solely for this, run first, and
are pure-mechanical with the existing suite as the pin. Their acceptance gates
assert the resulting line counts numerically.

### DA-3 — Render with no children / no notes / deep subtree

**Failure modes considered:**
- *No children, no notes:* `_section` would emit an empty section with a dangling
  header, producing unstable diffs. → **Mitigation (applied):** `_section` falls
  back to `EMPTY`; pinned by `test_render_node_with_no_children_and_no_notes_keeps_stable_structure`.
- *Deep subtree:* recursion would be unbounded and the cost superlinear. →
  **Mitigation (applied):** direct children only (spec §6.3's own wording), pinned
  by `test_collect_node_lists_only_direct_task_children`, which seeds a
  grandchild and asserts its absence.
- *Unknown state from a future migration:* `glyph_for` raising mid-render would
  make the whole notebook unreadable. → **Mitigation (applied):** `glyph_for` is
  TOTAL, pinned by `test_glyph_for_is_total_unknown_state_falls_back_to_pending`.
- *Multi-line note body:* naive rendering breaks the bullet list. →
  **Mitigation (applied):** continuation lines indented two spaces, pinned.

### DA-4 — Concurrent appends

**Failure mode:** two processes appending at once could interleave ids, or a
reader could see a partially-written file.

**Assessment:** the ids cannot collide — SQLite `AUTOINCREMENT` under the
connection's `busy_timeout` (set by `juggle_db_connect` for every connection)
serialises the INSERTs. Ordering is by `id`, never by `ts`, so same-second
appends stay deterministic (pinned by
`test_ordering_survives_same_second_writes`). The bigger real risk is **agent
processes opening the shared DB read-write at all** — which is exactly why Task 6
routes agent appends through the spool's single-writer broker.

**File-side mitigation (applied):** `materialize` writes to a PID-suffixed tmp
file and `os.replace`s it — atomic on both macOS and Linux for a same-directory
rename. Concurrent regenerates produce identical bytes (the render is pure), so
they converge; no reader ever sees a torn file; no tmp file is left behind
(pinned by `test_materialize_leaves_no_temp_files_behind`).

**RESIDUAL:** a genuinely concurrent two-process append test would be flaky in
CI, so the plan pins the *mechanism* (id ordering, atomic replace) rather than
racing threads. Stated, not hidden.

### DA-5 — Project aggregation with many open topics

**Failure mode:** N open topics ⇒ `collect_project` runs 1 + 3N queries and
`notebook show <project>` writes N files. At N=200 that is slow and noisy.

**Assessment:** it is bounded by `JUGGLE_MAX_THREADS` in practice, and the query
per topic is two indexed lookups (`idx_nodes_parent`, `idx_node_notes_node`)
plus one edge join. `_child_tasks` was **deliberately written as two queries for
the whole child set**, not one per child, which is where an N+1 would actually
have bitten.

**RESIDUAL:** spec §13 already flags "project notebook size" as future work
(cap/paginate). Not designed in — YAGNI. Raised in `--open-questions`.

### DA-6 — Path config when the directory does not exist

**Failure mode:** first-ever run on a fresh machine has no
`~/.claude/juggle/notebooks`; `write_text` raises `FileNotFoundError` and, worse,
that would surface *inside a dispatch*.

**Mitigation (applied):** `materialize` calls `mkdir(parents=True,
exist_ok=True)` first (pinned by `test_materialize_creates_missing_directories`,
which asserts the directory does **not** exist beforehand), and
`notebook_section_for_thread` wraps everything in a best-effort try/except
(pinned by `test_dispatch_survives_a_broken_notebook_layer`).

### DA-7 — Test pollution of the real notebooks directory

**Failure mode:** the file side effect is a *global* one. A test that renders
without an override writes into `~/.claude/juggle/notebooks` — the same class of
incident as the 2026-06-16 prod-DB pollution that `tests/conftest.py` now
fail-closed-guards.

**Mitigation (applied):** `JUGGLE_NOTEBOOKS_DIR` env override, set by an
`autouse` fixture in every notebook test file.
**RESIDUAL:** unlike the DB, there is no global fail-closed guard — a *future*
test that renders without the fixture would pollute. Raised in
`--open-questions` as "should conftest.py add a global notebooks-dir redirect?"
(recommended: yes, one autouse line, but it is scope creep on this plan).

### DA-8 — The WIP warning is nearly dead code for topic threads

**Failure mode:** `enforce_topic_gate` already refuses a topic completion while
any task is unmarked. So for graph topics the §9.2 warning can rarely fire — a
warning that never fires is worse than none, because it reads as coverage.

**Assessment:** it still fires for **ad-hoc conversation nodes with task
children** (no topic gate) and for topic threads whose children moved to
`dispatching`/`integrating` between the gate and the hook. The spec asks for it
explicitly and it costs ~8 lines.
**RESIDUAL:** documented here rather than silently shipped as though it were
broad coverage.

### DA-9 — Adding `append` to the closed verb vocabulary

**Failure mode:** `CLAUDE.md` says a regression pin may never be weakened. Adding
a verb to `CLOSED_VERBS` *looks* like weakening the P9 G3 lint.

**Assessment:** it is the lint's own documented extension mechanism ("New
commands MUST reuse a verb from this list; additions require updating the lint
allowlist"), and `test_lint_has_teeth_rejects_novel_verb` still proves the lint
bites afterwards. The alternative — renaming to `notebook create` or
`notebook update` — would actively misdescribe append-only semantics.
Raised in `--open-questions` for confirmation.

### DA-10 — The spec's `kind='conversation'` is wrong post-P8

**Failure mode:** a coder implementing §3 literally would filter
`kind='conversation'` and find **zero** child tasks for every graph topic
(post-Migration-53 those hang off `kind='topic'` nodes). The feature would
silently render empty Task sections for exactly the nodes that matter most.

**Mitigation (applied):** decision D1 makes the render kind-agnostic and the
project aggregate cover both kinds; `tests/test_notebook_collect.py` seeds a
`kind='topic'` node specifically so a regression to conversation-only filtering
fails loudly.

### DA-11 — The `[x]`/`[⊘]` view could disagree with the dispatcher

**Failure mode:** if the render used a looser dep-satisfaction set than the
scheduler (e.g. counting `cancelled` or `done` deps as met), a task would render
`[ ]` "ready" while the watchdog considers it blocked — the notebook would lie
about the one thing it exists to report.

**Mitigation (applied):** `DEP_SATISFIED_STATES = TERMINAL_SUCCESS_STATES`,
identical to `dbops.db_graph_edges.unverified_deps`, sourced from the shared
`dbops.terminal_states` module so a future change to the vocabulary moves both
together. Called out in a comment at the definition site.

---

# Open Questions (batched — do NOT block implementation)

Each has a locked default in this plan; each is a one-line reversal.

1. **Failure/cancelled glyphs.** Plan implements `[!]` for the four failure
   terminals (spec §13's own proposal) and `[-]` for cancelled/archived.
   Confirm as canon, or name different markers.
2. **Notebook directory default.** Plan uses
   `paths.notebooks_dir = ~/.claude/juggle/notebooks` (the plugin data-dir
   convention, per spec §13's recommendation) rather than the design's literal
   `~/.juggle/notebooks`, plus a `JUGGLE_NOTEBOOKS_DIR` env override. Confirm.
3. **Topic kind.** Spec §3 says a topic is `kind='conversation'`; post-P8 graph
   topics are `kind='topic'`. Plan renders kind-agnostically and aggregates both.
   Confirm (this is the only place the spec is factually stale post-P8).
4. **Project "open" set.** Plan excludes `done | archived | verified | delivered
   | cancelled` and keeps failure terminals visible, versus the spec's literal
   "not done/archived". Confirm.
5. **`who` vocabulary.** Free text, defaulted from caller context to
   `agent`/`orch`. Should v1 capture richer attribution (`agent:coder`) or
   constrain to an enum?
6. **Depth.** Direct children only (spec §6.3 wording). Should a topic with
   nested task groups render grandchildren, indented?
7. **CLI verb.** `append` added to the closed verb vocabulary via its documented
   extension mechanism. Confirm, versus renaming the verb.
8. **Global test guard for the notebooks dir.** Should `tests/conftest.py` gain
   an autouse redirect for `JUGGLE_NOTEBOOKS_DIR` (mirroring the prod-DB guard)
   so a future test can never write into the real notebooks directory? Plan
   scopes the override per-test-file instead; recommend yes as a follow-up.
9. **Project notebook size.** Spec §13 notes a large project produces a long
   aggregate. Plan renders all open topics with no cap. Confirm cap/pagination
   stays out of scope.
10. **Landing.** This adds a DB migration, so per `CLAUDE.md` landing policy it
    wants a PR rather than a routine ff-merge to main. Confirm the branch lands
    as one reviewed PR (Tasks 5 and 6 must not be split across PRs — see DA-1).
