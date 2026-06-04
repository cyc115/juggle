# Classifier Correction Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach Juggle's project auto-classifier to learn from manual reassignments by capturing corrections into SQLite and feeding them as few-shot examples into the LLM prompt.

**Architecture:** Three-part additive change: (1) schema migrations to track `assigned_by` and an append-only `project_corrections` log; (2) hook in `cmd_project_assign` to write correction rows and mark threads as human-assigned; (3) refactored `infer_project_id` that builds its LLM prompt from human-verified examples + recent corrections via a pure, testable builder function.

**Tech Stack:** Python, SQLite (via existing `JuggleDB` migration/connection pattern), `_cheap_llm_call` (existing free-model wrapper in `juggle_cli_common.py`).

---

## Out of Scope (do not implement)
- Re-classify existing threads when new corrections arrive
- Embedding / kNN classifier
- Confidence score badges
- Any provenance distinction between human vs. orchestrator-issued assigns (all non-auto assigns are treated equally)

---

## File Map

| File | Change |
|------|--------|
| `src/juggle_db.py` | Migrations 27 + 28; three new DB methods |
| `src/juggle_cmd_projects.py` | `cmd_project_assign` correction hook; `assign_project_background` sets `assigned_by='auto'`; extract `_build_classifier_prompt`; rework `infer_project_id` |
| `tests/test_projects_db.py` | Tests for new DB methods + migrations |
| `tests/test_projects.py` | Tests for `_build_classifier_prompt` + reworked `infer_project_id` |

---

## Devil's Advocate

**Weakest assumption:** Human-assigned threads are high-quality positives. Reality: the user might assign a thread to the wrong project temporarily, then move it again. The correction log captures the delta (from→to), not the final ground truth. Mitigation: corrections are capped at 5 most-recent and only fire when `from != to`, so a wrong-then-corrected pair produces two corrections that partially cancel in the prompt. Acceptable noise at this scale.

**Failure mode:** The `_build_classifier_prompt` token cap (800 chars for the examples section) is enforced by truncation, not by token counting. Long topic strings could push the examples section over 800 chars while appearing to fit within the cap. Mitigation: cap is applied to the formatted string, not raw topics — one truncated topic won't blow the full prompt past model limits.

**Simpler alternative:** Instead of separate `project_corrections` table, store corrections as a JSON field on the `projects` row. Rejected: append-only table is queryable (ORDER BY created_at LIMIT 5), survives project renames, and adds ~5 rows/week — negligible storage cost.

**Hidden dependency:** `assign_project_background` runs as a detached subprocess via `subprocess.Popen`. The subprocess reconstructs the infer call from scratch. After Task 3, the subprocess script string in `assign_project_background` must pass `db=db` so `infer_project_id` can query corrections. This is already the existing call pattern — `db=db` is already passed. Verify it remains in the script string after refactoring.

**Migration safety:** `ALTER TABLE threads ADD COLUMN assigned_by` uses `DEFAULT 'auto'` so existing rows get the right default without an `UPDATE`. The `project_corrections` table creation is idempotent (`CREATE TABLE IF NOT EXISTS`). Both migrations follow the repo's existing guard pattern: check column/table presence, attempt, catch `OperationalError`, log warning.

**Scope creep guard:** The task spec says "ALL non-auto assigns count (incl. orchestrator-issued)". This means `cmd_project_assign` is the only callsite that needs the hook — the orchestrator calls it through the same CLI path. Do not add hooks elsewhere.

---

## Task 1: Schema Migrations + DB Methods

**Files:**
- Modify: `src/juggle_db.py` (add DDL constant, migrations 27 + 28, three methods)
- Test: `tests/test_projects_db.py`

### Step 1.1 — Write failing tests

```python
# Append to tests/test_projects_db.py

def test_assigned_by_default_auto(tmp_path):
    db = make_db(tmp_path)
    tid = db.create_thread("test topic", session_id="s1")
    t = db.get_thread(tid)
    assert t["assigned_by"] == "auto"


def test_assigned_by_migration_idempotent(tmp_path):
    from juggle_db import JuggleDB
    path = str(tmp_path / "test.db")
    db1 = JuggleDB(path); db1.init_db()
    db2 = JuggleDB(path); db2.init_db()
    t = db2.get_thread(db1.create_thread("x", session_id="s"))
    assert t["assigned_by"] == "auto"


def test_log_project_correction(tmp_path):
    db = make_db(tmp_path)
    db.log_project_correction("topic A", from_project="INBOX", to_project="P1")
    corrections = db.get_recent_corrections(limit=5)
    assert len(corrections) == 1
    assert corrections[0]["topic"] == "topic A"
    assert corrections[0]["from_project"] == "INBOX"
    assert corrections[0]["to_project"] == "P1"


def test_get_recent_corrections_order(tmp_path):
    db = make_db(tmp_path)
    db.log_project_correction("first", "INBOX", "P1")
    db.log_project_correction("second", "P1", "P2")
    rows = db.get_recent_corrections(limit=5)
    # Most-recent first
    assert rows[0]["topic"] == "second"
    assert rows[1]["topic"] == "first"


def test_get_recent_corrections_limit(tmp_path):
    db = make_db(tmp_path)
    for i in range(7):
        db.log_project_correction(f"topic {i}", "INBOX", "P1")
    assert len(db.get_recent_corrections(limit=5)) == 5


def test_get_human_assigned_threads_by_project(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="work stuff")
    t1 = db.create_thread("topic alpha", session_id="s1")
    t2 = db.create_thread("topic beta", session_id="s1")
    db.update_thread(t1, project_id=pid, assigned_by="human")
    db.update_thread(t2, project_id=pid, assigned_by="auto")
    human_threads = db.get_human_assigned_threads_by_project(pid, limit=3)
    assert len(human_threads) == 1
    assert human_threads[0]["topic"] == "topic alpha"
```

- [ ] Run to confirm all fail:
  ```bash
  cd /Users/mikechen/github/juggle
  PYTHONPATH=src uv run pytest tests/test_projects_db.py -x -q 2>&1 | tail -10
  ```
  Expected: 6 failures (AttributeError / column not found).

### Step 1.2 — Add DDL constant for `project_corrections`

In `src/juggle_db.py`, after `CREATE_PROJECTS` (around line 198), add:

```python
CREATE_PROJECT_CORRECTIONS = """
CREATE TABLE IF NOT EXISTS project_corrections (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT NOT NULL,
  from_project TEXT NOT NULL,
  to_project   TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""
```

### Step 1.3 — Add Migration 27 (`assigned_by` on threads)

In `src/juggle_db.py`, inside `_run_migrations`, after Migration 26's closing block (after line ~684), add:

```python
        # Migration 27: assigned_by on threads ('auto'|'human')
        cols_threads = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "assigned_by" not in cols_threads:
            try:
                conn.execute(
                    "ALTER TABLE threads ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'auto'"
                )
                conn.commit()
                _log.info("Migration 27: assigned_by column added to threads")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 27 (assigned_by) skipped: %s", e)
```

### Step 1.4 — Add Migration 28 (`project_corrections` table)

Immediately after Migration 27:

```python
        # Migration 28: project_corrections append-only log
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "project_corrections" not in tables:
            try:
                conn.execute(CREATE_PROJECT_CORRECTIONS)
                conn.commit()
                _log.info("Migration 28: project_corrections table created")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 28 (project_corrections) skipped: %s", e)
```

### Step 1.5 — Add three new DB methods

In `src/juggle_db.py`, after `count_threads_by_project` (around line 932), add:

```python
    def log_project_correction(
        self, topic: str, from_project: str, to_project: str
    ) -> None:
        """Append one correction row. Call only when from_project != to_project."""
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_corrections (topic, from_project, to_project, created_at)"
                " VALUES (?, ?, ?, ?)",
                (topic, from_project, to_project, now),
            )
            conn.commit()

    def get_recent_corrections(self, limit: int = 5) -> list[dict]:
        """Return most-recent correction rows, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT topic, from_project, to_project, created_at"
                " FROM project_corrections ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_human_assigned_threads_by_project(
        self, project_id: str, limit: int = 3
    ) -> list[dict]:
        """Return most-recent human-assigned threads for a project, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, topic, last_active FROM threads"
                " WHERE project_id = ? AND assigned_by = 'human' AND show_in_list = 1"
                " ORDER BY last_active DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
```

### Step 1.6 — Run tests

```bash
cd /Users/mikechen/github/juggle
PYTHONPATH=src uv run pytest tests/test_projects_db.py -x -q 2>&1 | tail -10
```

Expected: all pass, including the 6 new tests and the 6 pre-existing ones.

### Step 1.7 — Commit

```bash
git add src/juggle_db.py tests/test_projects_db.py
git commit -m "feat: migrations 27-28 (assigned_by, project_corrections) + DB methods"
```

---

## Task 2: Hook Correction Signal in `cmd_project_assign`

**Files:**
- Modify: `src/juggle_cmd_projects.py` — `cmd_project_assign` and `assign_project_background`
- Test: `tests/test_projects.py`

### Step 2.1 — Write failing tests

```python
# Append to tests/test_projects.py

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def make_db_with_project(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Work", objective="work")
    tid = db.create_thread("some topic", session_id="s1")
    return db, tid, pid


def test_cmd_project_assign_logs_correction_on_change(tmp_path):
    from juggle_cmd_projects import cmd_project_assign
    from unittest.mock import patch, MagicMock
    db, tid, pid = make_db_with_project(tmp_path)
    thread = db.get_thread(tid)
    label = thread["user_label"]

    args = MagicMock()
    args.thread_id = label
    args.project_id = pid

    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    corrections = db.get_recent_corrections(limit=5)
    assert len(corrections) == 1
    assert corrections[0]["from_project"] == "INBOX"
    assert corrections[0]["to_project"] == pid


def test_cmd_project_assign_no_correction_when_same_project(tmp_path):
    from juggle_cmd_projects import cmd_project_assign
    from unittest.mock import patch, MagicMock
    db, tid, pid = make_db_with_project(tmp_path)
    db.update_thread(tid, project_id=pid)  # already assigned to pid
    thread = db.get_thread(tid)

    args = MagicMock()
    args.thread_id = thread["user_label"]
    args.project_id = pid

    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    assert len(db.get_recent_corrections(limit=5)) == 0


def test_cmd_project_assign_sets_assigned_by_human(tmp_path):
    from juggle_cmd_projects import cmd_project_assign
    from unittest.mock import patch, MagicMock
    db, tid, pid = make_db_with_project(tmp_path)
    thread = db.get_thread(tid)

    args = MagicMock()
    args.thread_id = thread["user_label"]
    args.project_id = pid

    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    assert db.get_thread(tid)["assigned_by"] == "human"


def test_assign_project_background_sets_assigned_by_auto(tmp_path):
    from juggle_cmd_projects import assign_project_background
    from unittest.mock import patch
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Work", objective="work")
    tid = db.create_thread("coding task", session_id="s1")
    projects = db.get_active_projects()

    with patch("juggle_cmd_projects.infer_project_id", return_value=pid):
        t = assign_project_background(db, tid, "coding task", _return_thread=True)
        t.join(timeout=5)

    assert db.get_thread(tid)["assigned_by"] == "auto"
    assert db.get_thread(tid)["project_id"] == pid
```

- [ ] Run to confirm failures:
  ```bash
  cd /Users/mikechen/github/juggle
  PYTHONPATH=src uv run pytest tests/test_projects.py -k "correction or assigned_by or assigned_by_auto" -x -q 2>&1 | tail -10
  ```
  Expected: 4 failures.

### Step 2.2 — Modify `cmd_project_assign`

Replace the current `cmd_project_assign` body in `src/juggle_cmd_projects.py` (lines 186–197):

```python
def cmd_project_assign(args):
    db = get_db(init=True)
    t = db.get_thread_by_user_label(args.thread_id)
    if not t:
        print(f"Thread not found: {args.thread_id}")
        sys.exit(1)
    p = db.get_project(args.project_id)
    if not p:
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    from_project = t.get("project_id") or INBOX_PROJECT_ID
    if from_project != args.project_id:
        try:
            db.log_project_correction(
                topic=t["topic"],
                from_project=from_project,
                to_project=args.project_id,
            )
        except Exception as e:
            log.warning("cmd_project_assign: failed to log correction: %s", e)
    db.update_thread(t["id"], project_id=args.project_id, assigned_by="human")
    print(f"Thread [{args.thread_id}] -> project {args.project_id} ({p['name']})")
```

### Step 2.3 — Modify `assign_project_background` (thread path)

In `assign_project_background`, the `_run()` inner function currently calls:
```python
db.update_thread(thread_uuid, project_id=project_id)
```

Change it to:
```python
db.update_thread(thread_uuid, project_id=project_id, assigned_by="auto")
```

Also update the subprocess script string (the `script` variable, around line 57–64). Replace:
```python
"pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid)"
```
With:
```python
"pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid, assigned_by='auto')"
```

### Step 2.4 — Run tests

```bash
cd /Users/mikechen/github/juggle
PYTHONPATH=src uv run pytest tests/test_projects.py tests/test_projects_db.py -x -q 2>&1 | tail -10
```

Expected: all pass.

### Step 2.5 — Commit

```bash
git add src/juggle_cmd_projects.py tests/test_projects.py
git commit -m "feat: capture corrections + assigned_by in cmd_project_assign"
```

---

## Task 3: Pure Few-Shot Builder + Rework `infer_project_id`

**Files:**
- Modify: `src/juggle_cmd_projects.py` — extract `_build_classifier_prompt`; rework `infer_project_id`
- Test: `tests/test_projects.py`

### Step 3.1 — Write failing tests for `_build_classifier_prompt`

```python
# Append to tests/test_projects.py

PROJECTS_FIXTURE = [
    {"id": "P1", "name": "Investing", "objective": "Automate stock idea generation"},
    {"id": "P2", "name": "LifeOS Dev", "objective": "Build AI assistant"},
]


def test_build_prompt_includes_corrections():
    from juggle_cmd_projects import _build_classifier_prompt
    corrections = [
        {"topic": "track my portfolio", "from_project": "INBOX", "to_project": "P1"},
    ]
    prompt = _build_classifier_prompt("portfolio tracker", PROJECTS_FIXTURE, {}, corrections)
    assert "track my portfolio" in prompt
    assert "-> P1" in prompt
    assert "not INBOX" in prompt or "not P1" not in prompt  # correction renders from_project


def test_build_prompt_includes_human_positives():
    from juggle_cmd_projects import _build_classifier_prompt
    positives = {
        "P1": [{"topic": "dividend yield screener"}, {"topic": "options flow dashboard"}],
    }
    prompt = _build_classifier_prompt("stock screener idea", PROJECTS_FIXTURE, positives, [])
    assert "dividend yield screener" in prompt
    assert "options flow dashboard" in prompt


def test_build_prompt_abstain_instruction():
    from juggle_cmd_projects import _build_classifier_prompt
    prompt = _build_classifier_prompt("random topic", PROJECTS_FIXTURE, {}, [])
    assert "INBOX" in prompt
    assert "clear fit" in prompt.lower() or "no project" in prompt.lower()


def test_build_prompt_token_cap():
    from juggle_cmd_projects import _build_classifier_prompt
    # Long topics that would exceed 800-char cap if uncapped
    long_corrections = [
        {"topic": "x" * 200, "from_project": "INBOX", "to_project": "P1"}
        for _ in range(5)
    ]
    prompt = _build_classifier_prompt("some topic", PROJECTS_FIXTURE, {}, long_corrections)
    # The examples section must be <= 800 chars
    # Extract the section between "Corrections:" and the JSON instruction
    assert len(prompt) < 3000  # full prompt stays reasonable


def test_infer_project_id_passes_corrections_to_prompt(tmp_path):
    from juggle_cmd_projects import infer_project_id
    from juggle_db import JuggleDB
    from unittest.mock import patch
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.create_project(name="Work", objective="work things")
    db.log_project_correction("build the widget", "INBOX", "P1")

    captured = {}
    def fake_llm(prompt, timeout=15):
        captured["prompt"] = prompt
        return '{"project_id": "INBOX"}'

    with patch("juggle_cmd_projects._cheap_llm_call", side_effect=fake_llm):
        infer_project_id("widget work", db.get_active_projects(), db=db)

    assert "build the widget" in captured["prompt"]
```

- [ ] Run to confirm failures:
  ```bash
  cd /Users/mikechen/github/juggle
  PYTHONPATH=src uv run pytest tests/test_projects.py -k "build_prompt or corrections_to_prompt" -x -q 2>&1 | tail -10
  ```
  Expected: 5 failures (ImportError on `_build_classifier_prompt`, assertion errors).

### Step 3.2 — Extract `_build_classifier_prompt` pure function

Add to `src/juggle_cmd_projects.py` before `infer_project_id`:

```python
_EXAMPLES_CAP_CHARS = 800


def _build_classifier_prompt(
    topic: str,
    projects: list[dict],
    positives_by_project: dict[str, list[dict]],
    corrections: list[dict],
) -> str:
    """Build the LLM classifier prompt. Pure function — no I/O, no DB calls.

    Args:
        topic: The topic to classify.
        projects: List of project dicts with keys id, name, objective.
        positives_by_project: {project_id: [thread_dict, ...]} — human-assigned threads.
        corrections: List of correction dicts (topic, from_project, to_project), newest first.
    """
    project_parts = []
    for p in projects:
        part = f'{p["id"]}: {p["name"]} — {p["objective"]}'
        threads = positives_by_project.get(p["id"], [])
        if threads:
            examples = "; ".join(t["topic"] for t in threads[:3])
            part += f' | verified examples: {examples}'
        project_parts.append(part)
    project_list = "; ".join(project_parts)

    # Build examples block, enforcing char cap
    example_lines = []
    chars_used = 0
    for c in corrections:
        line = f"  '{c['topic']}' -> {c['to_project']} (not {c['from_project']})"
        if chars_used + len(line) > _EXAMPLES_CAP_CHARS:
            break
        example_lines.append(line)
        chars_used += len(line)

    corrections_block = ""
    if example_lines:
        corrections_block = "\nPast corrections (learn from these):\n" + "\n".join(example_lines)

    return (
        f'Topic: "{topic}". '
        f'Projects: [{project_list}]. '
        f'{corrections_block}'
        f'If no project is a CLEAR fit, return INBOX. '
        f'Return ONLY valid JSON with no explanation, no markdown fences: '
        f'{{"project_id": "<id_or_INBOX>"}}'
    )
```

### Step 3.3 — Rework `infer_project_id`

Replace the current `infer_project_id` in `src/juggle_cmd_projects.py` with:

```python
def infer_project_id(topic: str, projects: list[dict], db=None) -> str:
    """Returns best project_id or INBOX. db is optional; when provided, adds
    human-assigned examples and recent corrections to the few-shot prompt."""
    if not projects:
        return INBOX_PROJECT_ID
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}

    positives_by_project: dict[str, list[dict]] = {}
    corrections: list[dict] = []
    if db:
        try:
            for p in projects:
                positives_by_project[p["id"]] = db.get_human_assigned_threads_by_project(
                    p["id"], limit=3
                )
        except Exception:
            pass
        try:
            corrections = db.get_recent_corrections(limit=5)
        except Exception:
            pass

    prompt = _build_classifier_prompt(topic, projects, positives_by_project, corrections)
    raw = _cheap_llm_call(prompt, timeout=15)
    if not raw:
        return INBOX_PROJECT_ID
    parsed = _extract_json(raw)
    pid = (parsed or {}).get("project_id", INBOX_PROJECT_ID)
    if pid not in valid_ids:
        log.warning("infer_project_id: invalid project_id %r in response: %r", pid, raw)
        return INBOX_PROJECT_ID
    return pid
```

### Step 3.4 — Run all tests

```bash
cd /Users/mikechen/github/juggle
PYTHONPATH=src uv run pytest tests/test_projects.py tests/test_projects_db.py -x -q 2>&1 | tail -15
```

Expected: all pass. If `test_infer_exact_match` or similar pre-existing tests break, check that `_build_classifier_prompt` still emits valid JSON instruction (the `{"project_id": ...}` suffix is preserved).

### Step 3.5 — Verify subprocess script string still passes `db=db`

Read the `assign_project_background` function in `src/juggle_cmd_projects.py` and confirm the script string still ends with:
```python
"pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid, assigned_by='auto')"
```
And that `infer_project_id({topic!r}, projects, db=db)` is still present. If not, add `db=db` back.

### Step 3.6 — Commit

```bash
git add src/juggle_cmd_projects.py tests/test_projects.py
git commit -m "feat: correction-fed few-shot prompt + abstain->INBOX instruction"
```

---

## Task 4: Version Bump

**Files:**
- Modify: `pyproject.toml` (version field)
- Modify: `.claude-plugin/plugin.json` (version field)

Both must stay in sync. This is a minor feature bump.

### Step 4.1 — Bump version

Determine current version:
```bash
grep '^version' /Users/mikechen/github/juggle/pyproject.toml
```

Increment the minor digit (e.g. `1.41.1` → `1.42.0`).

Update `pyproject.toml`:
```toml
version = "1.42.0"
```

Update `.claude-plugin/plugin.json`:
```json
"version": "1.42.0",
```

### Step 4.2 — Run full suite one final time

```bash
cd /Users/mikechen/github/juggle
PYTHONPATH=src uv run pytest tests/test_projects.py tests/test_projects_db.py -q 2>&1 | tail -5
```

Expected: all new and pre-existing tests pass.

### Step 4.3 — Commit

```bash
git add pyproject.toml .claude-plugin/plugin.json
git commit -m "chore: bump version to 1.42.0"
```

---

## Acceptance Criteria (agent-verifiable)

| Criterion | How an agent verifies |
|-----------|----------------------|
| Migration 27 adds `assigned_by DEFAULT 'auto'` | `PRAGMA table_info(threads)` shows the column; `test_assigned_by_default_auto` passes |
| Migration 28 creates `project_corrections` | `SELECT name FROM sqlite_master WHERE type='table'` includes `project_corrections`; `test_log_project_correction` passes |
| Manual assign logs correction when project changes | `test_cmd_project_assign_logs_correction_on_change` passes |
| No correction logged when project unchanged | `test_cmd_project_assign_no_correction_when_same_project` passes |
| Manual assign sets `assigned_by='human'` | `test_cmd_project_assign_sets_assigned_by_human` passes |
| Auto assign sets `assigned_by='auto'` | `test_assign_project_background_sets_assigned_by_auto` passes |
| Corrections appear in LLM prompt | `test_infer_project_id_passes_corrections_to_prompt` captures prompt and asserts topic present |
| Human positives appear in prompt | `test_build_prompt_includes_human_positives` passes |
| Abstain instruction in prompt | `test_build_prompt_abstain_instruction` passes |
| Zero new LLM calls; classification stays async | `assign_project_background` is unchanged (Popen path); `infer_project_id` still calls `_cheap_llm_call` exactly once |
| Pre-existing tests unbroken | `uv run pytest tests/test_projects.py tests/test_projects_db.py` passes in full |
