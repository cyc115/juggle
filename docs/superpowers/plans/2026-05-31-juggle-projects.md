# Juggle Projects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class Projects to Juggle — every thread auto-assigned to a project asynchronously, LLM coach wizard for project creation, cockpit grouped by project.

**Spec:** `docs/superpowers/specs/2026-05-31-juggle-projects-design.md`

**Branch:** `cyc_juggle-projects` off current `main`

**Execution order:** Tasks 1–8 are sequential (each depends on the previous).

---

## Files Touched

| File | Action | Notes |
|---|---|---|
| `src/juggle_cli_common.py` | Modify | Extract `_cheap_llm_call`, refactor `_generate_title_for_thread` |
| `src/juggle_db.py` | Modify | `projects` table DDL, INBOX seed, migration, DB methods |
| `src/juggle_cmd_projects.py` | Create | `infer_project_id`, `assign_project_background`, all CLI commands |
| `src/juggle_cmd_threads.py` | Modify | Call `assign_project_background` after insert in `cmd_create_thread` |
| `src/juggle_cli.py` | Modify | Wire `juggle project <subcmd>` subparser |
| `src/juggle_cockpit_model.py` | Modify | Attach project data to thread snapshot, add `group_threads_by_project` |
| `src/juggle_cockpit_view.py` | Modify | Render project section headers + grouped thread rows |
| `tests/test_cheap_llm_call.py` | Create | Unit tests for `_cheap_llm_call` |
| `tests/test_projects_db.py` | Create | DB migration + method tests |
| `tests/test_projects.py` | Create | `infer_project_id` unit tests + `assign_project_background` integration tests |
| `~/.claude/skills/juggle-start.md` (find path) | Modify | Add `juggle project` command reference |

---

## Task 1: Extract `_cheap_llm_call`

**Files:**
- Modify: `src/juggle_cli_common.py`
- Create: `tests/test_cheap_llm_call.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cheap_llm_call.py
from unittest.mock import patch, MagicMock
import pytest

def test_cheap_llm_call_returns_none_on_all_failures():
    from juggle_cli_common import _cheap_llm_call
    with patch("juggle_cli_common.subprocess.run", side_effect=Exception("fail")), \
         patch.dict("os.environ", {"OPENROUTER_KEY": ""}):
        result = _cheap_llm_call("test prompt", timeout=1)
    assert result is None

def test_cheap_llm_call_returns_haiku_result_when_openrouter_absent():
    from juggle_cli_common import _cheap_llm_call
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "some response"
    with patch("juggle_cli_common.subprocess.run", return_value=mock_result), \
         patch.dict("os.environ", {"OPENROUTER_KEY": ""}):
        result = _cheap_llm_call("test prompt", timeout=5)
    assert result == "some response"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_cheap_llm_call.py -v 2>&1 | tail -10
```
Expected: ImportError or AttributeError — `_cheap_llm_call` not defined yet.

- [ ] **Step 3: Add `_cheap_llm_call` to `juggle_cli_common.py`**

Add this function immediately before `_generate_title_for_thread`:

```python
def _cheap_llm_call(prompt: str, timeout: int = 10) -> str | None:
    """OpenRouter (Tier 1) -> Haiku subprocess (Tier 2) -> None on total failure.
    No DB side-effects. Caller decides what to do with None."""
    from juggle_settings import get_settings
    cfg = get_settings().get("title_gen", {})
    api_key = os.environ.get("OPENROUTER_KEY", "")
    if cfg.get("openrouter_enabled", True) and api_key:
        try:
            import urllib.request, json as _json
            body = _json.dumps({
                "model": cfg.get("openrouter_model", "meta-llama/llama-3.1-8b-instruct:free"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 50,
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.loads(resp.read())
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                logging.info("_cheap_llm_call: openrouter -> %r", text[:60])
                return text
        except Exception as e:
            logging.warning("_cheap_llm_call: openrouter failed: %s", e)
    try:
        haiku = cfg.get("haiku_model", "claude-haiku-4-5-20251001")
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", haiku],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            logging.info("_cheap_llm_call: haiku -> %r", result.stdout.strip()[:60])
            return result.stdout.strip()
    except Exception as e:
        logging.warning("_cheap_llm_call: haiku failed: %s", e)
    return None
```

Then update `_generate_title_for_thread` to call `_cheap_llm_call` instead of duplicating the tiers:

```python
def _generate_title_for_thread(db, thread_uuid: str, topic: str) -> str:
    from juggle_settings import get_settings
    cfg = get_settings().get("title_gen", {})
    fallback = " ".join(topic.replace("-", " ").replace("_", " ").split()[:5]).title()
    prompt = (
        f'Convert this task identifier into a concise 4-8 word descriptive title in Title Case. '
        f'Task: "{topic}". Reply with the title only. No punctuation. No quotes. No explanation. Use Title Case.'
    )
    timeout = cfg.get("timeout_secs", 10)

    def _valid(text: str) -> bool:
        if not text:
            return False
        words = text.split()
        return 3 <= len(words) <= 15 and "-" not in text and not all(w.islower() for w in words)

    title = _cheap_llm_call(prompt, timeout=timeout)
    if title and _valid(title):
        if not any(c.isupper() for c in title):
            title = title.title()
        logging.info("_generate_title_for_thread: -> %r", title)
        db.update_thread(thread_uuid, title=title)
        return title
    logging.info("_generate_title_for_thread: fallback -> %r", fallback)
    db.update_thread(thread_uuid, title=fallback)
    return fallback
```

- [ ] **Step 4: Run tests**

```bash
cd ~/github/juggle && uv run pytest tests/test_cheap_llm_call.py -v 2>&1 | tail -10
```
Expected: PASS (2 tests green).

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cli_common.py tests/test_cheap_llm_call.py && git commit -m "refactor: extract _cheap_llm_call from title_gen infrastructure"
```

---

## Task 2: DB — `projects` table, migration, methods

**Files:**
- Modify: `src/juggle_db.py`
- Create: `tests/test_projects_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_projects_db.py
import pytest
from pathlib import Path

def make_db(tmp_path):
    from juggle_db import JuggleDB
    return JuggleDB(str(tmp_path / "test.db"))

def test_inbox_seeded(tmp_path):
    db = make_db(tmp_path)
    p = db.get_project("INBOX")
    assert p is not None
    assert p["id"] == "INBOX"

def test_create_project_returns_p_label(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Test", objective="Do a thing")
    assert pid == "P1"
    assert db.get_project(pid)["name"] == "Test"
    assert db.get_project(pid)["status"] == "active"

def test_new_thread_gets_inbox(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_thread("my topic", session_id="s1")
    assert db.get_thread(tid)["project_id"] == "INBOX"

def test_migration_idempotent(tmp_path):
    from juggle_db import JuggleDB
    path = str(tmp_path / "test.db")
    db1 = JuggleDB(path)
    db2 = JuggleDB(path)
    assert db2.get_project("INBOX") is not None

def test_get_active_projects_excludes_inbox(tmp_path):
    db = make_db(tmp_path)
    db.create_project(name="P1", objective="obj1")
    projects = db.get_active_projects()
    assert all(p["id"] != "INBOX" for p in projects)
    assert any(p["name"] == "P1" for p in projects)

def test_count_threads_by_project(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    t1 = db.create_thread("task 1", session_id="s1")
    db.update_thread(t1, project_id=pid)
    assert db.count_threads_by_project(pid) == 1
    assert db.count_threads_by_project("INBOX") == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects_db.py -v 2>&1 | tail -15
```
Expected: AttributeError — `get_project` not defined.

- [ ] **Step 3: Add DDL constant to `juggle_db.py`**

Add after existing DDL constants (after `CREATE_MESSAGES`):

```python
INBOX_PROJECT_ID = "INBOX"

CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  objective        TEXT NOT NULL DEFAULT '',
  success_criteria TEXT NOT NULL DEFAULT '[]',
  out_of_scope     TEXT DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',
  created_at       TEXT NOT NULL,
  last_active      TEXT NOT NULL
);
"""
```

- [ ] **Step 4: Add migration in `_maybe_migrate`**

In the `_maybe_migrate` method, add at the end of the migration block:

```python
tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
cols_threads = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}

if "projects" not in tables:
    conn.execute(CREATE_PROJECTS)
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, objective, status, created_at, last_active) VALUES (?,?,?,?,?,?)",
        (INBOX_PROJECT_ID, "Inbox", "Catch-all for unassigned threads", "active", now, now),
    )

if "project_id" not in cols_threads:
    conn.execute("ALTER TABLE threads ADD COLUMN project_id TEXT DEFAULT 'INBOX' REFERENCES projects(id)")
    conn.execute("UPDATE threads SET project_id = 'INBOX' WHERE project_id IS NULL")
```

- [ ] **Step 5: Add DB methods to `JuggleDB` class**

```python
def _next_project_label(self, used: set) -> str:
    i = 1
    while True:
        label = f"P{i}"
        if label not in used:
            return label
        i += 1

def create_project(self, name: str, objective: str, success_criteria: str = "[]", out_of_scope: str = "") -> str:
    with self._conn() as conn:
        used = {r[0] for r in conn.execute("SELECT id FROM projects").fetchall()}
        pid = self._next_project_label(used)
        now = _now()
        conn.execute(
            "INSERT INTO projects (id,name,objective,success_criteria,out_of_scope,status,created_at,last_active) "
            "VALUES (?,?,?,?,?,'active',?,?)",
            (pid, name, objective, success_criteria, out_of_scope, now, now),
        )
    return pid

def get_project(self, project_id: str) -> dict | None:
    with self._conn() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

def list_projects(self, include_archived: bool = False) -> list[dict]:
    with self._conn() as conn:
        q = ("SELECT * FROM projects" if include_archived
             else "SELECT * FROM projects WHERE status != 'archived'")
        return [dict(r) for r in conn.execute(q).fetchall()]

def get_active_projects(self) -> list[dict]:
    """Active projects excluding INBOX — used for LLM assignment prompts."""
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE status = 'active' AND id != 'INBOX' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

def update_project(self, project_id: str, **kwargs) -> None:
    allowed = {"name", "objective", "success_criteria", "out_of_scope", "status", "last_active"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with self._conn() as conn:
        conn.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", (*fields.values(), project_id))

def count_threads_by_project(self, project_id: str) -> int:
    with self._conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE project_id = ? AND show_in_list = 1", (project_id,)
        ).fetchone()
        return row[0] if row else 0

def get_threads_by_project(self, project_id: str) -> list[dict]:
    with self._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM threads WHERE project_id = ? AND show_in_list = 1 ORDER BY last_active DESC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 6: Run tests**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects_db.py -v 2>&1 | tail -15
```
Expected: all 6 tests PASS.

- [ ] **Step 7: Commit**

```bash
cd ~/github/juggle && git add src/juggle_db.py tests/test_projects_db.py && git commit -m "feat(db): projects table, INBOX seed, project_id on threads, DB methods"
```

---

## Task 3: `infer_project_id` pure function

**Files:**
- Create: `src/juggle_cmd_projects.py` (skeleton)
- Create: `tests/test_projects.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_projects.py
from unittest.mock import patch
import pytest

PROJECTS = [
    {"id": "P1", "name": "Investing Automation", "objective": "Automate stock idea generation"},
    {"id": "P2", "name": "LifeOS Dev", "objective": "Build AI assistant platform"},
]

def test_infer_exact_match():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P1"}'):
        assert infer_project_id("automate investing ideas", PROJECTS) == "P1"

def test_infer_empty_projects_returns_inbox_without_llm_call():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call") as mock:
        result = infer_project_id("some topic", [])
    mock.assert_not_called()
    assert result == "INBOX"

def test_infer_unknown_project_id_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P99"}'):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"

def test_infer_llm_returns_inbox_sentinel():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "INBOX"}'):
        assert infer_project_id("random topic", PROJECTS) == "INBOX"

def test_infer_llm_returns_none_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=None):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"

def test_infer_invalid_json_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value="not json at all"):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects.py -v 2>&1 | tail -10
```
Expected: ModuleNotFoundError — `juggle_cmd_projects` not found.

- [ ] **Step 3: Create `src/juggle_cmd_projects.py` with `infer_project_id`**

```python
"""Juggle project management — CLI commands and background assignment."""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_cli_common import _cheap_llm_call

INBOX_PROJECT_ID = "INBOX"
log = logging.getLogger(__name__)


def infer_project_id(topic: str, projects: list[dict]) -> str:
    """Pure function — returns best project_id or INBOX. No DB, no threads, no side-effects."""
    if not projects:
        return INBOX_PROJECT_ID
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}
    project_list = "; ".join(f'{p["id"]}: {p["name"]} — {p["objective"]}' for p in projects)
    prompt = (
        f'Topic: "{topic}". '
        f'Projects: [{project_list}]. '
        f'Which project fits best? Return JSON only: {{"project_id": "<id_or_INBOX>"}}. No explanation.'
    )
    raw = _cheap_llm_call(prompt, timeout=5)
    if not raw:
        return INBOX_PROJECT_ID
    try:
        pid = json.loads(raw).get("project_id", INBOX_PROJECT_ID)
        return pid if pid in valid_ids else INBOX_PROJECT_ID
    except (json.JSONDecodeError, AttributeError):
        log.warning("infer_project_id: unparseable response: %r", raw)
        return INBOX_PROJECT_ID
```

- [ ] **Step 4: Run tests**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects.py -v 2>&1 | tail -10
```
Expected: all 6 PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_projects.py tests/test_projects.py && git commit -m "feat(projects): infer_project_id pure function with full unit test suite"
```

---

## Task 4: `assign_project_background` + wire into `thread create`

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `src/juggle_cmd_threads.py`
- Modify: `tests/test_projects.py`

- [ ] **Step 1: Add integration tests to `tests/test_projects.py`**

```python
def test_assign_project_background_updates_db(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    pid = db.create_project(name="Investing", objective="Automate stock ideas")
    tid = db.create_thread("automate investing ideas", session_id="s1")
    assert db.get_thread(tid)["project_id"] == "INBOX"
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=f'{{"project_id": "{pid}"}}'):
        t = assign_project_background(db, tid, "automate investing ideas", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["project_id"] == pid

def test_assign_project_background_silent_on_llm_failure(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    tid = db.create_thread("some topic", session_id="s1")
    with patch("juggle_cmd_projects._cheap_llm_call", side_effect=Exception("network error")):
        t = assign_project_background(db, tid, "some topic", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["project_id"] == "INBOX"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects.py::test_assign_project_background_updates_db -v 2>&1 | tail -10
```
Expected: AttributeError — `assign_project_background` not defined.

- [ ] **Step 3: Add `assign_project_background` to `juggle_cmd_projects.py`**

```python
def assign_project_background(
    db,
    thread_uuid: str,
    topic: str,
    _return_thread: bool = False,
) -> threading.Thread | None:
    """Fire-and-forget background project assignment.

    Failure contract: all exceptions caught and logged only. Thread stays INBOX.
    Never raises, never blocks, no user-visible side-effects on failure.
    _return_thread=True for testing only — returns Thread so caller can join.
    """
    def _run():
        try:
            projects = db.get_active_projects()
            project_id = infer_project_id(topic, projects)
            if project_id != INBOX_PROJECT_ID:
                db.update_thread(thread_uuid, project_id=project_id)
                log.info("assign_project_background: %s -> %s", thread_uuid[:8], project_id)
        except Exception as e:
            log.warning("assign_project_background: silent failure: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t if _return_thread else None
```

- [ ] **Step 4: Wire into `cmd_create_thread` in `juggle_cmd_threads.py`**

Add import at top of `juggle_cmd_threads.py`:
```python
from juggle_cmd_projects import assign_project_background
```

In `cmd_create_thread` (right after the title gen thread launch):
```python
    # Project assignment — async, fail-silent, never blocks
    assign_project_background(db, thread_uuid, args.topic)
```

- [ ] **Step 5: Run all project tests**

```bash
cd ~/github/juggle && uv run pytest tests/test_projects.py tests/test_projects_db.py tests/test_cheap_llm_call.py -v 2>&1 | tail -20
```
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_projects.py src/juggle_cmd_threads.py tests/test_projects.py && git commit -m "feat(projects): async background assignment wired into thread create, fail-silent"
```

---

## Task 5: CLI commands — `list`, `show`, `assign`, `edit`, `create`, `critique`

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `src/juggle_cli.py`

- [ ] **Step 1: Add `cmd_project_list` and `cmd_project_show` to `juggle_cmd_projects.py`**

```python
from rich.console import Console
from rich.table import Table
_console = Console()

def cmd_project_list(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    projects = db.list_projects()
    table = Table(title="Projects")
    table.add_column("ID", style="bold cyan")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Threads", justify="right")
    for p in sorted(projects, key=lambda x: (x["id"] == "INBOX", x["id"])):
        count = db.count_threads_by_project(p["id"])
        table.add_row(p["id"], p["name"], p["status"], str(count))
    _console.print(table)

def cmd_project_show(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    p = db.get_project(args.project_id)
    if not p:
        _console.print(f"[red]Project not found:[/red] {args.project_id}")
        sys.exit(1)
    criteria = json.loads(p.get("success_criteria") or "[]")
    _console.print(f"[bold cyan]{p['id']}[/bold cyan]  {p['name']}")
    _console.print(f"[dim]Status:[/dim]    {p['status']}")
    _console.print(f"[dim]Objective:[/dim] {p['objective']}")
    if criteria:
        _console.print("[dim]Success criteria:[/dim]")
        for c in criteria:
            _console.print(f"  - [ ] {c}")
    if p.get("out_of_scope"):
        _console.print(f"[dim]Out of scope:[/dim] {p['out_of_scope']}")
    threads = db.get_threads_by_project(p["id"])
    if threads:
        _console.print(f"\n[dim]Threads ({len(threads)}):[/dim]")
        for t in threads:
            _console.print(f"  [{t['user_label']}] {t['status']}  {t.get('title') or t['topic']}")
```

- [ ] **Step 2: Add `cmd_project_assign` and `cmd_project_edit`**

```python
def cmd_project_assign(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    t = db.get_thread_by_user_label(args.thread_id)
    if not t:
        print(f"Thread not found: {args.thread_id}")
        sys.exit(1)
    p = db.get_project(args.project_id)
    if not p:
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    db.update_thread(t["id"], project_id=args.project_id)
    print(f"Thread [{args.thread_id}] -> project {args.project_id} ({p['name']})")

def cmd_project_edit(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    if not db.get_project(args.project_id):
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    updates = {}
    if args.name:
        updates["name"] = args.name
    if args.objective:
        updates["objective"] = args.objective
    if args.out_of_scope is not None:
        updates["out_of_scope"] = args.out_of_scope
    if not updates:
        print("Nothing to update. Use --name, --objective, or --out-of-scope.")
        sys.exit(1)
    db.update_project(args.project_id, **updates)
    print(f"Project {args.project_id} updated.")
```

- [ ] **Step 3: Add coach wizard `cmd_project_create` and `cmd_project_critique`**

```python
def cmd_project_create(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    if args.force:
        if not args.name or not args.objective:
            print("--force requires --name and --objective")
            sys.exit(1)
        criteria = json.loads(args.success_criteria) if args.success_criteria else []
        pid = db.create_project(
            name=args.name, objective=args.objective,
            success_criteria=json.dumps(criteria), out_of_scope=args.out_of_scope or "",
        )
        print(f"Created project {pid}: {args.name}")
        return
    _run_project_coach(db)

def cmd_project_critique(args):
    from juggle_db import JuggleDB
    db = JuggleDB()
    if args.project_id == INBOX_PROJECT_ID:
        print("INBOX cannot be critiqued.")
        sys.exit(1)
    if not db.get_project(args.project_id):
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    _run_project_coach(db)

def _run_project_coach(db) -> None:
    """Multi-turn Sonnet coach wizard. Guides user to a well-defined project definition."""
    existing = db.get_active_projects()
    existing_summary = "; ".join(f'{p["id"]}: {p["name"]}' for p in existing) or "none"
    system = (
        "You are a project definition coach. Help the user define a clear, achievable project.\n"
        f"Existing projects: {existing_summary}\n\n"
        "Your job:\n"
        "1. Ask targeted questions (max 3 total) to understand what done looks like\n"
        "2. Flag if the idea sounds like multiple projects\n"
        "3. Propose a sharpened definition with objective + 2-3 measurable success criteria\n"
        "4. Ask about out-of-scope only if boundaries seem ambiguous\n\n"
        "When ready, output ONLY this JSON (no other text):\n"
        '{"ready": true, "name": "...", "objective": "...", "success_criteria": ["..."], "out_of_scope": "..."}\n\n'
        "Until ready, output ONLY your next question."
    )
    conversation = [{"role": "system", "content": system}]
    print("\nWhat's your project? (can be vague — I'll help you sharpen it)\n")
    user_input = input("> ").strip()
    if not user_input:
        print("Cancelled.")
        return
    conversation.append({"role": "user", "content": user_input})
    from juggle_settings import get_settings
    sonnet = get_settings().get("title_gen", {}).get("sonnet_model", "claude-sonnet-4-6")
    for _ in range(7):
        prompt = "\n".join(
            f'{"User" if m["role"]=="user" else ("System" if m["role"]=="system" else "Coach")}: {m["content"]}'
            for m in conversation
        ) + "\nCoach:"
        try:
            res = subprocess.run(["claude", "-p", prompt, "--model", sonnet],
                                 capture_output=True, text=True, timeout=30)
            response = res.stdout.strip() if res.returncode == 0 else None
        except Exception:
            response = None
        if not response:
            print("Coach unavailable. Use --force to skip the wizard.")
            return
        try:
            start = response.find("{")
            if start != -1:
                data = json.loads(response[start:])
                if data.get("ready"):
                    _confirm_and_save(db, data)
                    return
        except json.JSONDecodeError:
            pass
        print(f"\n{response}\n")
        conversation.append({"role": "assistant", "content": response})
        answer = input("> ").strip()
        if not answer:
            print("Cancelled.")
            return
        conversation.append({"role": "user", "content": answer})
    print("Could not converge. Use --force to skip the wizard.")

def _confirm_and_save(db, data: dict) -> None:
    print("\n── Draft Project ──────────────────────")
    print(f"Name:      {data['name']}")
    print(f"Objective: {data['objective']}")
    print("Success criteria:")
    for c in data.get("success_criteria", []):
        print(f"  - [ ] {c}")
    if data.get("out_of_scope"):
        print(f"Out of scope: {data['out_of_scope']}")
    print("───────────────────────────────────────")
    answer = input("\nApprove? [Y/n/edit] ").strip().lower()
    if answer in ("", "y", "yes"):
        pid = db.create_project(
            name=data["name"], objective=data["objective"],
            success_criteria=json.dumps(data.get("success_criteria", [])),
            out_of_scope=data.get("out_of_scope", ""),
        )
        print(f"\nCreated project {pid}: {data['name']}")
    elif answer == "edit":
        print("Press Enter to keep current value.")
        data["name"] = input(f"Name [{data['name']}]: ").strip() or data["name"]
        data["objective"] = input(f"Objective [{data['objective']}]: ").strip() or data["objective"]
        _confirm_and_save(db, data)
    else:
        print("Cancelled.")
```

- [ ] **Step 4: Wire subparser into `juggle_cli.py`**

Add imports at top of `juggle_cli.py` (with existing imports):
```python
from juggle_cmd_projects import (
    cmd_project_list, cmd_project_show, cmd_project_assign,
    cmd_project_edit, cmd_project_create, cmd_project_critique,
)
```

Add after existing subparsers (after the last `set_defaults` block):
```python
# juggle project <subcmd>
p_project = subparsers.add_parser("project", help="Manage projects")
_ps = p_project.add_subparsers(dest="project_command", required=True)

_p = _ps.add_parser("list"); _p.set_defaults(func=cmd_project_list)
_p = _ps.add_parser("show"); _p.add_argument("project_id"); _p.set_defaults(func=cmd_project_show)
_p = _ps.add_parser("assign"); _p.add_argument("thread_id"); _p.add_argument("project_id"); _p.set_defaults(func=cmd_project_assign)
_p = _ps.add_parser("edit"); _p.add_argument("project_id"); _p.add_argument("--name"); _p.add_argument("--objective"); _p.add_argument("--out-of-scope", dest="out_of_scope"); _p.set_defaults(func=cmd_project_edit)
_p = _ps.add_parser("create"); _p.add_argument("--force", action="store_true"); _p.add_argument("--name"); _p.add_argument("--objective"); _p.add_argument("--success-criteria", dest="success_criteria"); _p.add_argument("--out-of-scope", dest="out_of_scope", default=""); _p.set_defaults(func=cmd_project_create)
_p = _ps.add_parser("critique"); _p.add_argument("project_id"); _p.set_defaults(func=cmd_project_critique)
```

- [ ] **Step 5: Smoke test all commands**

```bash
cd ~/github/juggle && uv run python src/juggle_cli.py project list
```
Expected: table with INBOX row.

```bash
uv run python src/juggle_cli.py project create --force --name "Test" --objective "Validate CLI wiring"
```
Expected: "Created project P1: Test"

```bash
uv run python src/juggle_cli.py project show P1
```
Expected: project card printed.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_projects.py src/juggle_cli.py && git commit -m "feat(projects): all CLI commands wired (list/show/assign/edit/create/critique)"
```

---

## Task 6: Cockpit — group thread list by project

**Files:**
- Modify: `src/juggle_cockpit_model.py`
- Modify: `src/juggle_cockpit_view.py`

- [ ] **Step 1: Add project data to cockpit snapshot in `juggle_cockpit_model.py`**

After the existing thread queries, add project loading:

```python
# Load projects for grouping
project_rows = conn.execute(
    "SELECT id, name FROM projects WHERE status != 'archived' ORDER BY (id='INBOX'), id"
).fetchall()
projects_by_id: dict[str, str] = {r["id"]: r["name"] for r in project_rows}
```

Attach `project_id` and `project_name` to each loaded thread dict:
```python
for t in (active_threads + running_threads + background_threads + closed_threads):
    t["project_id"] = t.get("project_id") or "INBOX"
    t["project_name"] = projects_by_id.get(t["project_id"], "Inbox")
```

Store on the snapshot object:
```python
snapshot.projects_by_id = projects_by_id  # add this field to the snapshot dataclass/namedtuple
```

Also add this helper function in the model module:

```python
def group_threads_by_project(
    threads: list[dict], projects_by_id: dict[str, str]
) -> list[tuple[str, str, list[dict]]]:
    """Returns [(project_id, project_name, threads)] sorted: named projects first, INBOX last."""
    from collections import defaultdict
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in threads:
        groups[t.get("project_id", "INBOX")].append(t)
    result = []
    for pid, name in sorted(projects_by_id.items(), key=lambda x: (x[0] == "INBOX", x[0])):
        if pid in groups:
            result.append((pid, name, groups[pid]))
    if "INBOX" in groups and "INBOX" not in projects_by_id:
        result.append(("INBOX", "Inbox", groups["INBOX"]))
    return result
```

- [ ] **Step 2: Update thread list render in `juggle_cockpit_view.py`**

Find the thread list rendering loop. Wrap the existing flat iteration with project grouping:

```python
from juggle_cockpit_model import group_threads_by_project
from rich.text import Text

# Replace flat thread loop with grouped render
groups = group_threads_by_project(model.active_threads, model.projects_by_id)
rows = []
for project_id, project_name, threads in groups:
    count = len(threads)
    header = Text()
    header.append(f"▸ {project_name.upper()}", style="bold white")
    header.append(f"  {count} active", style="dim")
    rows.append(header)
    for t in threads:
        rows.append(render_thread_row(t))  # existing render function, unchanged
```

- [ ] **Step 3: Run cockpit and verify grouping**

```bash
cd ~/github/juggle && uv run python src/juggle_cockpit.py
```
Expected: thread list shows `▸ INBOX  N active` header(s) grouping threads below.

- [ ] **Step 4: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cockpit_model.py src/juggle_cockpit_view.py && git commit -m "feat(cockpit): group thread list by project with section headers"
```

---

## Task 7: Update `juggle:start` skill

**Files:**
- Modify: the `juggle:start` skill file (find path below)

- [ ] **Step 1: Find the skill file**

```bash
find ~/.claude/skills ~/.claude/plugins -name "*.md" 2>/dev/null | xargs grep -l "juggle.*start\|start.*juggle\|thread create" 2>/dev/null | head -5
```

- [ ] **Step 2: Add project commands block**

In the CLI commands section, add:

````markdown
### Project Commands

```
juggle project create           — define a new project via LLM coach wizard (interactive)
juggle project list             — list all projects with active thread counts
juggle project show <id>        — full project card + assigned threads
juggle project assign <t> <id>  — manually assign a thread to a project
juggle project critique <id>    — re-run LLM coach on an existing project
juggle project edit <id>        — update name, objective, or out-of-scope
```

Auto-assignment: every new thread is silently assigned to the best-matching project in
the background. No action needed. Failures are silent — thread stays in Inbox.
````

- [ ] **Step 3: Commit**

```bash
cd ~/github/juggle && git add -A && git commit -m "docs: update juggle:start skill with project subcommands"
```

---

## Task 8: Run full test suite + version bump

- [ ] **Step 1: Run all tests**

```bash
cd ~/github/juggle && uv run pytest tests/ -v 2>&1 | tail -30
```
Expected: all project tests + existing tests PASS. Note any pre-existing failures with `git stash && pytest && git stash pop` to confirm they predate this branch.

- [ ] **Step 2: Bump version (minor — new feature)**

Find version in `pyproject.toml`:
```bash
grep "^version" ~/github/juggle/pyproject.toml
```
Increment the minor version (e.g. `1.36.4` -> `1.37.0`). Update `pyproject.toml` and `CHANGELOG.md`.

- [ ] **Step 3: Final commit**

```bash
cd ~/github/juggle && git add pyproject.toml CHANGELOG.md && git commit -m "chore: bump version for juggle projects feature"
```
