# Multi-Project Parallel Autopilot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Arm a SET of projects for autopilot; one watchdog tick fairly dispatches ready nodes across all armed graphs under the global agent budget; cockpit + hooks reflect the set; disarming one project leaves the rest running.

**Architecture:** The settings key `autopilot_armed_project` becomes a CSV ordered list (1-element value ≡ today's scalar → zero migration). A new `juggle_autopilot_state.py` owns set accessors; a new pure `juggle_graph_scheduler.py` orders ready nodes least-loaded-first with round-robin interleave; `graph_tick` loops per-project sweep/recompute then runs one interleaved dispatch pass. Spec: `docs/specs/2026-06-10-multi-project-autopilot.md` (read it first — esp. the Devil's Advocate section).

**Tech Stack:** Python 3 + pytest + sqlite (existing `JuggleDB`), Rich/Textual cockpit. Run everything with `uv run`.

**Conventions for every task:**
- Work from repo root. Tests: `uv run pytest -q tests/<file> -v`.
- TDD: write the failing test, SEE it fail, implement, SEE it pass, commit.
- Regression pins must name the incident in their docstring (date + symptom).
- Pre-existing failures on the base commit are not your concern — note them, move on.

---

### Task 0: Preflight — baseline green + WL assumption check

**Files:** none (read-only)

- [ ] **Step 1: Record the baseline**

```bash
uv run pytest -q tests/test_graph_dispatch.py tests/test_cmd_autopilot.py tests/test_graph_status.py tests/test_cockpit_graph_dag_load.py 2>&1 | tail -3
```

Expected: all pass (e.g. `XX passed`). If anything fails HERE, on the untouched base, record the exact failures in your completion notes as pre-existing and continue.

- [ ] **Step 2: Verify the WL dispatch-visibility assumption**

```bash
git log --oneline -15 | head -15
git log --grep="visib" --grep="cross-connection" -i --oneline | head -5
```

Expected: a commit referencing the thread-WL dispatch cross-connection-visibility fix exists. If you cannot find it, do NOT block — this plan only builds on top of it; note its absence in the completion summary.

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
    assert st.arm_project(db, "a") == ["a", "b"]  # idempotent, order kept
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
    assert st.get_armed_projects(db) == ["b"]
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
Must not own: dispatching (juggle_graph_dispatch), scheduling
(juggle_graph_scheduler), or the CLI surface (juggle_cmd_autopilot).
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
    """Remove ``pid`` from the armed set (absent → no-op). Returns the new set."""
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

In `src/juggle_graph_dispatch.py`: delete the `ARMED_PROJECT_KEY = ...` line (line 27) and the whole `get_armed_project` function (lines 43–49); add near the top imports:

```python
from juggle_autopilot_state import (  # noqa: F401 — re-exported, existing importers
    ARMED_PROJECT_KEY,
    get_armed_project,
    get_armed_projects,
)
```

- [ ] **Step 5: Run new + existing tests**

```bash
uv run pytest -q tests/test_autopilot_state.py tests/test_graph_dispatch.py tests/test_cmd_autopilot.py -v 2>&1 | tail -5
```

Expected: ALL PASS (existing suites import `ARMED_PROJECT_KEY`/`get_armed_project` from `juggle_graph_dispatch` — the re-export keeps them green).

- [ ] **Step 6: Commit**

```bash
git add src/juggle_autopilot_state.py src/juggle_graph_dispatch.py tests/test_autopilot_state.py
git commit -m "feat: armed-project SET accessors (CSV in existing settings key)

Multi-project autopilot step 1. 1-element CSV == legacy scalar: no migration.
juggle_graph_dispatch re-exports for back-compat."
```

---

### Task 2: Fair scheduler — pure `juggle_graph_scheduler.py`

**Files:**
- Create: `src/juggle_graph_scheduler.py`
- Test: `tests/test_graph_scheduler.py` (new)

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for juggle_graph_scheduler — least-loaded-first round-robin interleave
(multi-project autopilot fairness policy, spec 2026-06-10 §2.4). Pure function,
no DB."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_graph_scheduler import interleave_ready  # noqa: E402


def _n(i):  # minimal node dict — scheduler must only rely on identity
    return {"id": i}


def test_empty_input():
    assert interleave_ready({}, {}, []) == []


def test_single_project_order_preserved():
    ready = {"p1": [_n("a"), _n("b"), _n("c")]}
    out = interleave_ready(ready, {"p1": 0}, ["p1"])
    assert out == [("p1", _n("a")), ("p1", _n("b")), ("p1", _n("c"))]


def test_round_robin_interleave_two_projects():
    ready = {"p1": [_n("a1"), _n("a2"), _n("a3")], "p2": [_n("b1"), _n("b2")]}
    out = interleave_ready(ready, {"p1": 0, "p2": 0}, ["p1", "p2"])
    assert [(p, n["id"]) for p, n in out] == [
        ("p1", "a1"), ("p2", "b1"), ("p1", "a2"), ("p2", "b2"), ("p1", "a3"),
    ]


def test_least_loaded_project_goes_first():
    """REGRESSION PIN (2026-06-10): with budget 1/tick, arm-order round-robin
    starved every project but the first — least-loaded-first must put the
    project with fewer in-flight nodes ahead, statelessly."""
    ready = {"p1": [_n("a1")], "p2": [_n("b1")]}
    out = interleave_ready(ready, {"p1": 2, "p2": 0}, ["p1", "p2"])
    assert [(p, n["id"]) for p, n in out] == [("p2", "b1"), ("p1", "a1")]


def test_tie_break_is_arm_order():
    ready = {"p2": [_n("b1")], "p1": [_n("a1")]}
    out = interleave_ready(ready, {"p1": 1, "p2": 1}, ["p1", "p2"])
    assert [p for p, _ in out] == ["p1", "p2"]


def test_fifty_vs_two_budget_five_prefix_is_fair():
    """Spec §2.4 50-vs-2 case: the first 5 interleaved entries must contain
    BOTH of the small project's nodes (small graph drains fully)."""
    ready = {
        "big": [_n(f"x{i}") for i in range(50)],
        "small": [_n("s1"), _n("s2")],
    }
    out = interleave_ready(ready, {"big": 0, "small": 0}, ["big", "small"])
    first5 = [(p, n["id"]) for p, n in out[:5]]
    assert ("small", "s1") in first5 and ("small", "s2") in first5
    assert len(out) == 52


def test_missing_in_flight_counts_default_zero():
    ready = {"p1": [_n("a1")]}
    out = interleave_ready(ready, {}, ["p1"])
    assert out == [("p1", _n("a1"))]


def test_projects_without_ready_entries_are_skipped():
    out = interleave_ready({"p1": []}, {"p1": 0}, ["p1", "ghost"])
    assert out == []
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_scheduler.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'juggle_graph_scheduler'`.

- [ ] **Step 3: Implement `src/juggle_graph_scheduler.py`**

```python
"""juggle_graph_scheduler — fair cross-project dispatch ordering (pure).

Owns: the least-loaded-first round-robin interleave that decides in which
ORDER ready nodes are claimed when several projects are armed. The global
capacity cap (MAX_THREADS / agent pool) stays where it is — enforced by the
dispatch path; because this ordering is fair, the prefix that fits under the
cap is fair too (a cap hit breaks the whole pass, spec DA A5).
Must not own: DB access, claiming, or dispatching (juggle_graph_dispatch).

Policy (spec 2026-06-10 §2.4): sort armed projects by current in-flight node
count ascending (tie-break: arm order), then emit ready nodes one per project
per round. Stateless + deterministic — no persisted cursor, self-balancing
across ticks because last tick's winners carry higher in-flight counts.
"""

from __future__ import annotations


def interleave_ready(
    ready_by_project: dict[str, list[dict]],
    in_flight: dict[str, int],
    armed_order: list[str],
) -> list[tuple[str, dict]]:
    """Fair cross-project dispatch order: list of (project_id, node)."""
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

- [ ] **Step 4: Run to verify pass**

```bash
uv run pytest -q tests/test_graph_scheduler.py -v
```

Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add src/juggle_graph_scheduler.py tests/test_graph_scheduler.py
git commit -m "feat: pure fair scheduler — least-loaded-first round-robin interleave

Multi-project autopilot step 2 (spec §2.4). Stateless, deterministic."
```

---

### Task 3: CLI — `arm` adds, `disarm [P]` / `off [P]`, multi `status`

**Files:**
- Modify: `src/juggle_cmd_autopilot.py`
- Test: `tests/test_cmd_autopilot.py` (append)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_cmd_autopilot.py`; reuse its existing `db_path`/`db`/`flag` fixtures and `_args` helper — note `_args` already takes `project`)

```python
# ── multi-project arming (2026-06-10) ─────────────────────────────────────────


def test_arm_second_project_adds_not_replaces(db_path, db, flag, capsys):
    """REGRESSION PIN (2026-06-10): arming a second project silently REPLACED
    the first (scalar set_setting overwrite) — arm must ADD to the set."""
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


def test_off_one_project_keeps_flag_while_others_armed(db_path, db, flag, capsys):
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    ap.cmd_autopilot(_args(db_path, "off", "INBOX"))
    assert db.get_setting(ARMED_PROJECT_KEY) == "p2"
    assert flag.exists(), "flag clears only when the set becomes empty"
    ap.cmd_autopilot(_args(db_path, "off", "p2"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert not flag.exists()


def test_off_no_arg_clears_everything(db_path, db, flag, capsys):
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    ap.cmd_autopilot(_args(db_path, "off"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert not flag.exists()


def test_status_lists_every_armed_project(db_path, db, flag, capsys):
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "Armed projects (2): INBOX, p2" in out
    assert "INBOX:" in out and "p2:" in out


def test_status_json_multi_plus_deprecated_compat_fields(db_path, db, flag, capsys):
    import json as _json
    db.create_project("p2", name="P2")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "arm", "p2"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    payload = _json.loads(capsys.readouterr().out)
    assert payload["armed_projects"] == ["INBOX", "p2"]
    assert set(payload["graphs"].keys()) == {"INBOX", "p2"}
    assert payload["armed_project"] == "INBOX"  # deprecated, one release
    assert payload["diverged"] is False
```

If `db.create_project` does not exist with that signature, check `src/dbops/` for the projects API (`grep -n "def create_project\|def add_project" src/dbops/*.py src/juggle_db.py`) and use the real one — the test intent is unchanged.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_cmd_autopilot.py -v 2>&1 | tail -15
```

Expected: the new tests FAIL (arm overwrites; disarm has no project arg; status prints `Armed project:` singular). Pre-existing tests still pass.

- [ ] **Step 3: Implement in `src/juggle_cmd_autopilot.py`**

Changes (keep PR-mode refusal, project-exists check, flag semantics, graphs_dir hints exactly as-is):

```python
# add import at top:
from juggle_autopilot_state import (
    arm_project,
    disarm_project,
    get_armed_projects,
    set_armed_projects,
)
```

In `_cmd_arm`, replace `db.set_setting(ARMED_PROJECT_KEY, project_id)` with:

```python
    try:
        armed = arm_project(db, project_id)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
```

and replace the first print with:

```python
    others = [p for p in armed if p != project_id]
    suffix = f" Armed set: {', '.join(armed)}." if others else ""
    print(f"AUTOPILOT ON — project {project_id} ({project['name']}) armed.{suffix}")
```

Rewrite `_cmd_status`:

```python
def _cmd_status(db, json_out: bool) -> None:
    from juggle_graph_status import format_progress, graph_counts

    global_on = AUTOPILOT_FLAG.exists()
    armed = get_armed_projects(db)
    graphs = {pid: graph_counts(db, pid) for pid in armed}
    diverged = bool(armed) and not global_on
    if json_out:
        first = armed[0] if armed else None
        print(json.dumps({
            "global_on": global_on,
            "armed_projects": armed,
            "graphs": graphs,
            "diverged": diverged,
            # deprecated compat fields (remove after one release):
            "armed_project": first,
            "graph": graphs.get(first) if first else None,
        }))
        return
    print(f"Autopilot global: {'ON' if global_on else 'OFF'}")
    if not armed:
        print("Armed projects: (none)")
    else:
        print(f"Armed projects ({len(armed)}): {', '.join(armed)}")
        for pid in armed:
            c = graphs[pid]
            print(f"  {pid}: {format_progress(c) if c else 'no graph loaded'}")
    if diverged:
        print(
            "WARNING: settings key and flag file diverge — project(s) "
            f"{', '.join(armed)} armed but the global flag ({AUTOPILOT_FLAG}) "
            "is OFF: hooks inject nothing while the tick still dispatches. "
            f"Run `juggle autopilot arm {armed[0]}` to restore the flag, "
            "or `juggle autopilot off`."
        )
```

Rewrite the `disarm`/`off` branches in `cmd_autopilot`:

```python
    if cmd == "arm":
        _cmd_arm(db, args.project)
    elif cmd == "disarm":
        _cmd_remove(db, args.project, clear_flag_when_empty=False)
    elif cmd == "on":
        _flag_set(True)
        print("AUTOPILOT ON (global). No project armed — use: juggle autopilot arm <project>")
    elif cmd == "off":
        _cmd_remove(db, args.project, clear_flag_when_empty=True)
    else:
        _cmd_status(db, getattr(args, "json_out", False))
```

with the shared helper:

```python
def _cmd_remove(db, project_id: str | None, *, clear_flag_when_empty: bool) -> None:
    """disarm/off: remove one project (or all), with set-aware flag handling."""
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
    flag_state = "ON" if AUTOPILOT_FLAG.exists() else "OFF"
    what = f"Project {project_id} disarmed." if project_id else "All projects disarmed."
    print(f"{what}{rest} Global autopilot: {flag_state}.")
```

In `register()`, give `disarm` and `off` an optional positional:

```python
    for name, hlp in (
        ("disarm", "Disarm a project (or all) — global flag unchanged"),
        ("off", "Disarm a project (or all); flag clears when set is empty"),
    ):
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("project", nargs="?", default=None,
                        help="Project id (omit = all armed projects)")
        sp.set_defaults(func=cmd_autopilot)
    sp_on = sub.add_parser("on", help="Global autopilot ON (flag cache only)")
    sp_on.set_defaults(func=cmd_autopilot, project=None)
```

(Replace the existing 3-tuple loop; `on` keeps `project=None` default.)

- [ ] **Step 4: Run tests**

```bash
uv run pytest -q tests/test_cmd_autopilot.py -v 2>&1 | tail -5
```

Expected: ALL PASS — including the pre-existing single-project tests (R6). If an old test asserts the exact legacy `disarm` message text, update its assertion to the new message (behavior contract — flag untouched — is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_autopilot.py tests/test_cmd_autopilot.py
git commit -m "feat: autopilot CLI arms a SET — arm adds, disarm/off take optional project

Multi-project autopilot step 3. status lists every armed graph; JSON adds
armed_projects/graphs, keeps deprecated armed_project/graph one release."
```

---

### Task 4: Multi-project tick in `graph_tick`

**Files:**
- Modify: `src/juggle_graph_dispatch.py` (`graph_tick` only)
- Test: `tests/test_graph_dispatch.py` (append)

- [ ] **Step 1: Write the failing tests** (append; reuse the file's `db` fixture, `_mk`, `_arm`, `FakeDispatch`. Add a project-aware maker + armer)

```python
# ── multi-project tick (2026-06-10) ───────────────────────────────────────────


def _mkp(db, node_id, project, deps=()):
    g.create_node(db, node_id=node_id, project_id=project,
                  title=f"Node {node_id}", prompt=f"do {node_id}")
    if deps:
        g.replace_edges(db, node_id, list(deps))


def _arm_many(db, *projects):
    db.set_setting(gd.ARMED_PROJECT_KEY, ",".join(projects))


def test_tick_dispatches_across_all_armed_projects(db):
    """REGRESSION PIN (2026-06-10): the tick only served get_armed_project()
    (first/scalar) — a second armed project's ready nodes were never
    dispatched. Every armed graph must be ticked."""
    _mkp(db, "a1", "P1")
    _mkp(db, "b1", "P2")
    _arm_many(db, "P1", "P2")
    fd = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fd)
    assert sorted(stats["dispatched"]) == ["a1", "b1"]
    assert g.get_node(db, "a1")["state"] == "running"
    assert g.get_node(db, "b1")["state"] == "running"


def test_each_thread_bound_to_its_nodes_project(db):
    """REGRESSION PIN (2026-06-10 spec DA weakest-item): cross-project thread
    mis-binding — a node's thread tagged with ANOTHER armed project would
    hydrate the wrong objective. Each created thread's project_id must match
    its node's."""
    _mkp(db, "a1", "P1")
    _mkp(db, "b1", "P2")
    _arm_many(db, "P1", "P2")
    gd.graph_tick(db, dispatch_fn=FakeDispatch())
    for nid, pid in (("a1", "P1"), ("b1", "P2")):
        tid = g.get_node(db, nid)["thread_id"]
        assert tid, f"{nid} should be thread-bound"
        assert db.get_thread(tid)["project_id"] == pid


def test_disarm_one_mid_batch_other_project_keeps_dispatching(db):
    """REGRESSION PIN (2026-06-10): the old mid-batch guard compared the whole
    armed scalar and stopped EVERYTHING — disarming one project must skip only
    that project's remaining nodes."""
    _mkp(db, "a1", "P1")
    _mkp(db, "a2", "P1")
    _mkp(db, "b1", "P2")
    _mkp(db, "b2", "P2")
    _arm_many(db, "P1", "P2")

    class DisarmingDispatch(FakeDispatch):
        def __call__(self, db_, thread_id, prompt, node):
            super().__call__(db_, thread_id, prompt, node)
            if node["id"].startswith("a"):  # first P1 dispatch disarms P1
                db_.set_setting(gd.ARMED_PROJECT_KEY, "P2")

    stats = gd.graph_tick(db, dispatch_fn=DisarmingDispatch())
    dispatched = set(stats["dispatched"])
    assert {"b1", "b2"} <= dispatched, "P2 must be unaffected by P1 disarm"
    assert len({"a1", "a2"} & dispatched) == 1, "P1 stops after the disarm"


def test_poisoned_project_scan_does_not_block_others(db, monkeypatch):
    """REGRESSION PIN (2026-06-10 spec DA / R4): a ready-scan exception used to
    abort the WHOLE tick; with N projects the blast radius must be one graph."""
    _mkp(db, "a1", "P1")
    _mkp(db, "b1", "P2")
    _arm_many(db, "P1", "P2")
    real = g.recompute_ready

    def boom(db_, pid):
        if pid == "P1":
            raise RuntimeError("poisoned graph")
        return real(db_, pid)

    monkeypatch.setattr(gd.db_graph, "recompute_ready", boom)
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())
    assert stats["dispatched"] == ["b1"]


def test_global_cap_defers_fairly_across_projects(db, monkeypatch):
    """Capacity is GLOBAL: when the agent pool fills mid-pass, remaining nodes
    of ALL projects defer with claims released — and because the order was
    interleaved, the dispatched prefix contains BOTH projects (R3)."""
    for i in range(3):
        _mkp(db, f"a{i}", "P1")
    _mkp(db, "b0", "P2")
    _arm_many(db, "P1", "P2")

    class CapAfter(FakeDispatch):
        def __init__(self, n):
            super().__init__()
            self.n = n
        def __call__(self, db_, thread_id, prompt, node):
            if len(self.calls) >= self.n:
                raise gd.CapacityError("pool full")
            super().__call__(db_, thread_id, prompt, node)

    stats = gd.graph_tick(db, dispatch_fn=CapAfter(2))
    assert len(stats["dispatched"]) == 2
    projects = {nid[0] for nid in stats["dispatched"]}  # 'a'→P1, 'b'→P2
    assert projects == {"a", "b"}, "fair prefix: both projects dispatched"
    deferred_states = [g.get_node(db, n)["state"] for n in stats["deferred"]]
    assert all(s == "ready" for s in deferred_states), "claims released on defer"


def test_single_project_tick_behavior_unchanged(db):
    """R6 pin: a 1-element armed set behaves exactly like the legacy scalar."""
    _mk(db, "a")
    _mk(db, "b", deps=("a",))
    _arm(db)  # legacy scalar arm helper
    fd = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fd)
    assert stats["dispatched"] == ["a"]  # b gated on dep, exactly as before
```

If `db.get_thread(tid)` is not the real accessor, find it (`grep -n "def get_thread" src/dbops/*.py src/juggle_db.py`) and adapt.

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_graph_dispatch.py -k "multi or across or poisoned or disarm_one or cap_defers or bound_to_its" -v
```

Expected: FAIL — multi-armed CSV reads as one bogus project id, so nothing dispatches (`stats["dispatched"] == []`).

- [ ] **Step 3: Rewrite `graph_tick` in `src/juggle_graph_dispatch.py`**

Replace the whole `graph_tick` function with:

```python
def graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict:
    """One dispatcher tick across ALL armed projects. Never raises.

    Per project: stale-claim sweep + ready recompute (a failure skips ONLY
    that project — R4). Ready nodes are then ordered fairly across projects
    (juggle_graph_scheduler: least-loaded-first round-robin) and dispatched
    through the existing claim → thread → hydrate → running body. Capacity
    (MAX_THREADS / pool) is global, so a cap hit defers the rest of the pass;
    the fair ordering makes the dispatched prefix fair (R3).
    """
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
            stats["swept"] += sweep_stale_claims(db, pid)
            # Self-heal: promote eligible pending nodes (idempotent) — covers a
            # completion that crashed between marking and ready-recompute.
            db_graph.recompute_ready(db, pid)
            nodes = db_graph.list_nodes(db, pid)
        except Exception:
            _log.exception(
                "graph tick: ready-set scan failed for %s — skipping project", pid
            )
            continue
        ready_by_project[pid] = [n for n in nodes if n["state"] == "ready"]
        in_flight[pid] = sum(1 for n in nodes if n["state"] in IN_FLIGHT_STATES)

    for pid, node in interleave_ready(ready_by_project, in_flight, armed):
        node_id = node["id"]
        if pid not in get_armed_projects(db):
            continue  # THIS project disarmed mid-batch — others keep going
        try:
            if not claim_node(db, node_id):
                continue  # another claimer won (DA B4)
            try:
                thread_id = db.create_thread(
                    f"[{node_id}] {node['title']}"[:80], session_id=_session_id(db)
                )
            except ValueError as e:
                db_graph.node_transition(db, node_id, "stale_reset")
                if "Maximum of" not in str(e):
                    # NOT the MAX_THREADS cap (DA round-2 minor 6, 2026-06-10:
                    # unrelated ValueErrors were silently deferred forever).
                    stats["errors"].append(node_id)
                    db.add_action_item(
                        thread_id=None,
                        message=(
                            f"⚠️ Autopilot thread creation failed for graph "
                            f"node {node_id}: {e}"
                        ),
                        type_="failure",
                        priority="high",
                    )
                    continue
                # MAX_THREADS cap is GLOBAL — defer this and every later node.
                stats["deferred"].append(node_id)
                _log.info("graph tick: thread cap hit — node %s deferred", node_id)
                break
            db.update_thread(thread_id, project_id=pid)
            # Bind BEFORE send-task (DA round-2 MAJOR-4, 2026-06-10): a crash
            # in the dispatch window must leave the node thread-bound so the
            # stale sweep cannot reclaim it and double-dispatch the work.
            db_graph.set_node_thread(db, node_id, thread_id)
            fail_key = (str(db.db_path), node_id)
            try:
                dispatch(db, thread_id, _hydrate_for_node(db, pid, node), node)
            except CapacityError:
                db.archive_thread(thread_id)
                db_graph.set_node_thread(db, node_id, None)
                db_graph.node_transition(db, node_id, "stale_reset")
                stats["deferred"].append(node_id)
                break  # capacity is global — later nodes would hit it too
            except Exception as e:
                db.archive_thread(thread_id)
                db_graph.set_node_thread(db, node_id, None)
                stats["errors"].append(node_id)
                fails = _dispatch_fails.get(fail_key, 0) + 1
                _dispatch_fails[fail_key] = fails
                if fails >= MAX_DISPATCH_FAILS:
                    _dispatch_fails.pop(fail_key, None)
                    _give_up_dispatch(db, node_id, e)
                else:
                    db_graph.node_transition(db, node_id, "stale_reset")
                    db.add_action_item(
                        thread_id=None,
                        message=(
                            f"⚠️ Autopilot dispatch failed for graph node "
                            f"{node_id} (attempt {fails}/{MAX_DISPATCH_FAILS}): {e}"
                        ),
                        type_="failure",
                        priority="high",
                    )
                continue
            _dispatch_fails.pop(fail_key, None)
            db_graph.node_transition(db, node_id, "dispatch")  # → running
            db.add_notification_v2(
                thread_id=thread_id,
                message=f"⬢ autopilot dispatched graph node {node_id} — {node['title']}",
                session_id=_session_id(db),
            )
            stats["dispatched"].append(node_id)
        except Exception:
            # Belt-and-braces: a tick must never take the watchdog down.
            _log.exception("graph tick: unexpected error on node %s", node_id)
            stats["errors"].append(node_id)
    return stats
```

The per-node body is UNCHANGED from the current code except: `armed` → `pid` (thread binding + hydration), the mid-batch guard, and the break-on-defer behavior accounting deferred nodes after a cap break is unchanged (nodes never claimed stay `ready`; only the node that hit the cap is appended to `deferred` — matching today's contract). On the `CapacityError` and `Maximum of` breaks, remaining interleaved entries are simply not visited — they were never claimed, so they need no release.

- [ ] **Step 4: Run the dispatch suite**

```bash
uv run pytest -q tests/test_graph_dispatch.py -v 2>&1 | tail -5
```

Expected: ALL PASS, new and old. Old single-project tests are the R6 pins — do NOT weaken them.

- [ ] **Step 5: LOC gate check**

```bash
wc -l src/juggle_graph_dispatch.py
```

Expected: ≤ ~310 lines (the function grew by ~15 but the accessor block moved to `juggle_autopilot_state` in Task 1). If meaningfully over 300, extract the per-node dispatch body into a private `_dispatch_one(db, dispatch, pid, node, stats) -> str` helper ("dispatched"/"deferred-break"/"continue") in the same file as a separate mechanical-refactor commit, tests green before and after.

- [ ] **Step 6: Commit**

```bash
git add src/juggle_graph_dispatch.py tests/test_graph_dispatch.py
git commit -m "feat: graph_tick drives every armed project with fair interleave

Multi-project autopilot step 4. Per-project sweep/recompute isolation (R4),
least-loaded-first round-robin order (R3), per-node disarm guard, global cap
break preserved. Single-project pins unchanged (R6)."
```

---

### Task 5: Hooks inject the full armed set (R7)

**Files:**
- Modify: `src/juggle_hooks_autopilot.py`
- Test: `tests/test_juggle_hooks.py` if armed-context tests live there (check: `grep -ln "_armed_graph_context\|ARMED_CARVEOUT" tests/*.py`) — otherwise create `tests/test_hooks_autopilot_multi.py` with the tests below.

NOTE: some hook tests run against the SHARED DB (`~/.claude/juggle/juggle.db`, see CLAUDE.md). The tests below avoid that by monkeypatching `_cfg.get_db` to a tmp DB.

- [ ] **Step 1: Write the failing tests** (`tests/test_hooks_autopilot_multi.py`)

```python
"""Hooks must inject the FULL armed set (R7, multi-project autopilot
2026-06-10) with the graph-status budget split across projects."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_hooks_autopilot as ha  # noqa: E402
import juggle_hooks_config as _cfg  # noqa: E402
from juggle_autopilot_state import ARMED_PROJECT_KEY  # noqa: E402


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "hooks.db"))
    d.init_db()
    monkeypatch.setattr(_cfg, "get_db", lambda: d)
    return d


def _node(db, nid, pid):
    g.create_node(db, node_id=nid, project_id=pid, title=nid, prompt=f"do {nid}")


def test_carveout_names_every_armed_project(db):
    """REGRESSION PIN (2026-06-10): the carve-out formatted ONE project — an
    agent could believe project 2's nodes were free to dispatch manually."""
    _node(db, "a1", "P1")
    _node(db, "b1", "P2")
    db.set_setting(ARMED_PROJECT_KEY, "P1,P2")
    ctx = ha._armed_graph_context()
    assert "P1, P2" in ctx.splitlines()[0]
    assert "Graph [P1]" in ctx and "Graph [P2]" in ctx


def test_injection_budget_split_keeps_total_bounded(db):
    for pid in ("P1", "P2", "P3"):
        for i in range(12):
            _node(db, f"{pid}-n{i:02d}", pid)
        g.recompute_ready(db, pid)
    db.set_setting(ARMED_PROJECT_KEY, "P1,P2,P3")
    ctx = ha._armed_graph_context()
    graph_lines = [l for l in ctx.splitlines() if l.startswith("Graph [")]
    assert len(graph_lines) == 3
    assert sum(len(l) for l in graph_lines) <= 540  # 3 × max(160, 500//3)


def test_disarmed_returns_empty(db):
    assert ha._armed_graph_context() == ""
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_hooks_autopilot_multi.py -v
```

Expected: FAIL — single-project formatting (`ARMED PROJECT P1,P2:` treated as one id; only one `Graph [` line).

- [ ] **Step 3: Implement in `src/juggle_hooks_autopilot.py`**

Replace `_ARMED_CARVEOUT` and `_armed_graph_context`:

```python
_ARMED_CARVEOUT = (
    "ARMED PROJECTS {projects}: nodes of any armed project are tick-owned — "
    "NEVER dispatch them manually; report status only. The watchdog tick "
    "claims, dispatches, and completes graph nodes; manual send-task to "
    "node-bound threads is refused without --force-node."
)


def _armed_graph_context() -> str:
    """Carve-out + budgeted graph status for EVERY armed project, else ''.

    Authority is the ``autopilot_armed_project`` settings key (DA M6) — now a
    CSV set. The per-project injection budget is the 500-char discipline split
    across the set (floor 160) so total stays bounded for any N.
    Degrades to '' on any DB error — the base directive must survive.
    """
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

- [ ] **Step 4: Run hook tests**

```bash
uv run pytest -q tests/test_hooks_autopilot_multi.py -v
uv run pytest -q tests/test_juggle_hooks.py -v 2>&1 | tail -3
```

Expected: new file ALL PASS. `test_juggle_hooks.py` needs the shared-DB setup from CLAUDE.md; if its failures match the documented pre-existing isolation limitation (or the same failures exist on base), note and move on — but any NEW failure caused by the carve-out wording change (e.g. a test asserting `ARMED PROJECT `) must be updated to the new plural wording.

- [ ] **Step 5: Commit**

```bash
git add src/juggle_hooks_autopilot.py tests/test_hooks_autopilot_multi.py
git commit -m "feat: hooks inject full armed set with split graph-status budget (R7)"
```

---

### Task 6: Cockpit shows all armed graphs (R5)

**Files:**
- Modify: `src/juggle_cockpit_graph_dag.py` (CSV parse + `load_graph_dags`)
- Modify: `src/juggle_cockpit_model.py` (`graph_dags` on `CockpitState`)
- Modify: `src/juggle_cockpit_graph_panel.py` (`build_multi_graph_panel`)
- Modify: `src/juggle_cockpit_graph_mode.py` (`_render_graph_panel` uses the list)
- Test: `tests/test_cockpit_graph_dag_load.py` (append), `tests/test_cockpit_graph_panel.py` (append)

- [ ] **Step 1: Write the failing loader tests** (append to `tests/test_cockpit_graph_dag_load.py`, matching its existing fixture style — read the file first and reuse its conn/db setup helpers):

```python
def test_load_graph_dags_returns_one_per_armed_project_with_nodes(...):
    """REGRESSION PIN (2026-06-10): the DAG loader read the armed key as a
    scalar — with 'P1,P2' armed it queried a nonexistent project id and the
    graph panel went blank. Must return one GraphDag per armed project that
    has nodes, in arm order."""
    # arrange: nodes for P1 and P2, none for P3; settings key = "P1,P2,P3"
    # act:    dags = load_graph_dags(conn)
    # assert: [d.project_id for d in dags] == ["P1", "P2"]


def test_load_graph_dag_shim_returns_first(...):
    # settings key = "P1,P2" → load_graph_dag(conn).project_id == "P1"


def test_load_graph_dags_empty_when_disarmed(...):
    # no key → load_graph_dags(conn) == []
```

Write these as REAL tests (full arrange/act/assert) using the existing fixtures in that file — the comments above specify the contract, the surrounding tests show the mechanics (they create `graph_nodes` rows and a settings row on a tmp sqlite conn).

- [ ] **Step 2: Run to verify failure**

```bash
uv run pytest -q tests/test_cockpit_graph_dag_load.py -v 2>&1 | tail -5
```

Expected: FAIL — `ImportError: cannot import name 'load_graph_dags'`.

- [ ] **Step 3: Implement loader in `src/juggle_cockpit_graph_dag.py`**

Refactor: extract the existing per-project body (everything after the armed lookup) into `_load_one(conn, pid) -> GraphDag | None`, then:

```python
def _armed_set(conn) -> list[str]:
    """CSV-parse the armed key (mirrors juggle_autopilot_state — cockpit reads
    raw SQL by design, no src-package import)."""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (ARMED_PROJECT_SETTING,)
        ).fetchone()
    except Exception:
        return []
    raw = ((row[0] if row else "") or "")
    out: list[str] = []
    for part in raw.split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def load_graph_dags(conn) -> "list[GraphDag]":
    """One GraphDag per armed project that has nodes, in arm order."""
    return [d for pid in _armed_set(conn) if (d := _load_one(conn, pid))]


def load_graph_dag(conn) -> "GraphDag | None":
    """COMPAT SHIM: first armed project's DAG (legacy single-graph callers)."""
    dags = load_graph_dags(conn)
    return dags[0] if dags else None
```

- [ ] **Step 4: Model — add `graph_dags` to the snapshot**

In `src/juggle_cockpit_model.py`: import `load_graph_dags` alongside the existing dag import; add to `CockpitState`:

```python
    graph_dags: "list | None" = None  # ALL armed DAGs, only in graph mode
```

and where `snapshot(..., load_graph_dag=True)` currently sets `graph_dag=...`, set both: `graph_dags = _load_graph_dags(conn)` and `graph_dag = graph_dags[0] if graph_dags else None` (the shim field keeps `juggle_cockpit_screenshot.py` and old readers alive — do not remove it this release).

- [ ] **Step 5: Panel — failing test then `build_multi_graph_panel`** (append to `tests/test_cockpit_graph_panel.py`, reusing its render helpers):

```python
def test_multi_panel_stacks_each_armed_dag_with_header(...):
    """REGRESSION PIN (2026-06-10): graph panel rendered only the first armed
    DAG. With two dags, the rendered text must contain both project headers
    ('P1 ·' and 'P2 ·') separated by a rule."""
    # build two small GraphDag-shaped inputs; call build_multi_graph_panel;
    # render to text via the file's existing console-capture helper; assert
    # both 'P1 ·' and 'P2 ·' appear, P1 before P2.
```

(Write as a real test using the file's existing GraphNode/console plumbing.) Then implement in `src/juggle_cockpit_graph_panel.py` — extract the body of `build_graph_panel` after the title into `_graph_section(project_id, nodes, edges, sel_id, inner_w, pan_offset) -> list` (header + grid + optional minimap, i.e. lines 106–137 unchanged), and add:

```python
def build_multi_graph_panel(
    *,
    dags: list,  # objects with .project_id/.nodes/.edges (GraphDag)
    selection: int,
    unread: int,
    width: int,
    height: int,
    pan_offset: int,
) -> Panel:
    """Stacked multi-DAG panel: one titled section per armed graph.

    selection indexes the concatenated flat selectable list across dags in
    arm order (within a dag: rank-major, id order — same as single).
    """
    title = f"Graph{_badge_segment(unread)}"
    if not dags:
        body = Text(
            "no armed graph — arm a project with /juggle:toggle-autopilot",
            style=Style(dim=True),
        )
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
        parts.extend(
            _graph_section(d.project_id, d.nodes, d.edges, sel_id, inner_w, pan_offset)
        )
    return Panel(_Group(*parts), title=title, border_style="cyan")
```

`build_graph_panel` itself becomes a thin wrapper over `_graph_section` for its single case — pure-mechanical extraction, existing panel tests must stay green unmodified.

- [ ] **Step 6: Mode mixin — use the list**

In `src/juggle_cockpit_graph_mode.py` `_render_graph_panel`, replace the body with:

```python
        from juggle_cockpit_graph_panel import build_multi_graph_panel

        dags = getattr(state, "graph_dags", None) or (
            [state.graph_dag] if getattr(state, "graph_dag", None) else []
        )
        try:
            w = self.query_one("#notifications").size.width or 80
            h = self.query_one("#notifications").size.height or 20
        except Exception:
            w, h = 80, 20
        total = sum(len(d.nodes) for d in dags)
        self._graph_sel = min(self._graph_sel, max(0, total - 1))
        return build_multi_graph_panel(
            dags=dags, selection=self._graph_sel, unread=self._graph_unread,
            width=w, height=h, pan_offset=self._graph_pan,
        )
```

Check the rest of the mixin (`_graph_select`, detail modal — `sed -n '80,139p' src/juggle_cockpit_graph_mode.py`) for other `state.graph_dag` / `dag.nodes` uses and route them through the same concatenated list (selection order must match `build_multi_graph_panel`'s `flat`).

- [ ] **Step 7: Run cockpit suites + smoke matrix**

```bash
uv run pytest -q tests/test_cockpit_graph_dag_load.py tests/test_cockpit_graph_panel.py tests/test_cockpit_graph.py tests/test_cockpit_graph_keys.py tests/test_cockpit_graph_layout.py -v 2>&1 | tail -5
uv run src/juggle_cli.py cockpit --smoke --all-viewports
uv run src/juggle_cli.py cockpit --out | head -40
```

Expected: tests ALL PASS; smoke harness reports all 7 viewport profiles green (overflow / real-estate / chrome checks); `--out` renders without traceback.

- [ ] **Step 8: Commit**

```bash
git add src/juggle_cockpit_graph_dag.py src/juggle_cockpit_model.py src/juggle_cockpit_graph_panel.py src/juggle_cockpit_graph_mode.py tests/test_cockpit_graph_dag_load.py tests/test_cockpit_graph_panel.py
git commit -m "feat: cockpit graph mode stacks every armed project's DAG (R5)"
```

---

### Task 7: Full gates, version bump, bookkeeping

**Files:**
- Modify: `.claude-plugin/plugin.json` (version), `TODO.md`

- [ ] **Step 1: Full harness smoke gate (CLAUDE.md mandatory)**

```bash
export _JUGGLE_TEST_DB="$HOME/.claude/juggle/juggle.db"
export CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"
export JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run pytest -q 2>&1 | tail -3
uv run src/juggle_cli.py doctor --dry-run
uv run src/juggle_cli.py cockpit --smoke --all-viewports
```

Expected: pytest summary green except failures you PROVED pre-exist on the base commit (re-run those exact tests on base to prove it; list them in the completion summary). Doctor dry-run and viewport smoke clean. **Paste the pytest summary line into your completion result — completion claims without harness evidence are invalid.**

- [ ] **Step 2: End-to-end CLI scenario (agent-verifiable, deterministic)**

```bash
T=$(mktemp -d); export _JUGGLE_TEST_DB="$T/e2e.db"
uv run src/juggle_cli.py init-db
uv run python - <<'EOF'
import os, sys
sys.path.insert(0, "src")
from juggle_db import JuggleDB
db = JuggleDB(db_path=os.environ["_JUGGLE_TEST_DB"])
for pid in ("p1", "p2"):
    try:
        db.create_project(pid, name=pid.upper())
    except Exception as e:
        print("adjust to real projects API:", e); raise
EOF
uv run src/juggle_cli.py autopilot arm p1
uv run src/juggle_cli.py autopilot arm p2
uv run src/juggle_cli.py autopilot status --json
uv run src/juggle_cli.py autopilot off p1
uv run src/juggle_cli.py autopilot status --json
```

Expected: first JSON has `"armed_projects": ["p1", "p2"]`; second has `["p2"]` and `"global_on": true`. (Adapt project creation to the real API found in Task 3.)

- [ ] **Step 3: Version bump + TODO + graphify**

`.claude-plugin/plugin.json`: bump minor (e.g. `1.60.x` → `1.61.0` — feature). `TODO.md`: mark the multi-project autopilot item done (`- [x] … ✅ 2026-06-10`, Done section) or add it done if absent. Then:

```bash
graphify update . || true   # AST-only graph refresh; non-fatal if unavailable
git add .claude-plugin/plugin.json TODO.md graphify-out/ 2>/dev/null
git commit -m "feat: multi-project parallel autopilot (v1.61.0)

Arm a SET of projects; fair least-loaded round-robin tick across all armed
graphs; cockpit stacks DAGs; hooks inject the set. Specs:
docs/specs/2026-06-10-multi-project-autopilot.md"
```

- [ ] **Step 4: Devil's-advocate pass (CLAUDE.md directive)**

Re-read spec §3 (Devil's Advocate) and confirm each mitigation actually landed: A1 comma guard (Task 1 test), A2 grep gate — run it now:

```bash
grep -rn "autopilot_armed_project" src/ | grep -v "juggle_autopilot_state.py\|juggle_cockpit_graph_dag.py"
```

Expected: only re-export/imports and docstrings — no remaining raw scalar READER of the key. Any hit that parses the value directly is a bug; fix before completing.

---

## Devil's Advocate (plan-level): weakest assumption per task

| Task | Weakest assumption | Failure mode | Mitigation |
|---|---|---|---|
| 1 | All importers go through the re-export | A direct scalar reader survives | Task 7 Step 4 grep gate |
| 2 | In-flight count proxies load | Long-running nodes deprioritize a project with urgent work | Accepted by design (spec DA A4); policy swappable behind the pure function |
| 3 | No external script parses status JSON beyond the kept fields | Script breaks on new keys | Additive keys only; deprecated fields retained one release |
| 4 | Per-node body transplant is faithful | Cross-project thread mis-binding → wrong-repo agent work | Pinned test `test_each_thread_bound_to_its_nodes_project`; single-project pins unmodified |
| 5 | 160-char floor is readable | 4+ projects → truncated-but-present lines | Bounded by design; ellipsis truncation is deterministic (existing DA m4) |
| 6 | Stacked DAGs fit small viewports | Overflow in 80×67 third-pane | Viewport smoke matrix is the merge gate; panel clips per existing rules |
| 7 | Pre-existing shared-DB hook-test failures are the documented isolation limitation | Masking a real new failure | Prove each failure exists on base before dismissing |

## Open questions

None — all brief questions resolved in the spec (§2.4, DA). No `--open-questions` batch.
