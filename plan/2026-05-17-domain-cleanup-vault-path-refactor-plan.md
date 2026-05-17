# Domain Cleanup + Vault-Path Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove dead `domain` machinery (CLI commands, DB columns, tables, registry methods, `get_best_agent` filter) and move the vault-path lookup from `domains.initial_domain_paths` to first-class `paths.vault` / `paths.vault_name` keys. Ship `/juggle:doctor` as the auto-migration helper for users with custom `config.json`.

**Architecture:** Hard cutover — no in-code fallback for the old `domains` block. Migrations 17–19 drop the column + tables; `juggle_settings.DEFAULTS` loses the `domains` key entirely; `juggle_db.py` module-level seed vars are removed (otherwise the import-time `_get_settings()["domains"]` read crashes). `/juggle:doctor` rewrites the user's `config.json` and runs the DB migrations. Version bumps 1.15.x → 1.16.0.

**Tech Stack:** Python 3.11+, SQLite 3.35+ (`ALTER TABLE DROP COLUMN`), `pytest`, juggle's existing presence-based migration framework (no `schema_version` table).

**Spec:** `~/github/juggle/docs/superpowers/specs/2026-05-17-domain-cleanup-vault-path-refactor.md`
**Status:** Implemented 2026-05-17 (v1.21.0)

**Commit policy (per Mike's directive `feedback_juggle_commit_to_main`):** Commit directly to `main`. No feature branch, no PR. Each task below ends in a commit.

**Pre-flight (one-time, run before Task 1):**

```bash
cp ~/.claude/juggle/juggle.db ~/.claude/juggle/juggle.db.bak-pre-1.16
cp ~/.juggle/config.json ~/.juggle/config.json.bak-pre-1.16 2>/dev/null || true
```

The first command is the rollback artifact required by the spec's Section 2 "Rollback" note. The second is belt-and-suspenders before we test `/juggle:doctor` against the live config.

---

## File Structure

**Modified:**

- `src/juggle_settings.py` — drop `DEFAULTS["domains"]`, extend `DEFAULTS["paths"]` with `vault` + `vault_name`.
- `src/juggle_db.py` — drop `CREATE_DOMAINS`/`CREATE_DOMAIN_PATHS` constants + `init_db()` calls; drop module-level `_INITIAL_DOMAINS`/`_INITIAL_DOMAIN_PATHS`; empty Migration 9 body; add Migrations 17–19; drop registry methods (`register_domain`, `get_domains`, `is_known_domain`, `add_domain_path`, `get_domain_paths`, `infer_domain_from_prompt`); simplify `get_best_agent`.
- `src/juggle_cli.py` — rewrite `_get_vault_root` / `_get_vault_name` (no fallback); remove `--domain` arg, `register-domain` + `register-domain-path` subparsers and their imports; add `doctor` subparser + dispatch.
- `src/juggle_cmd_research.py` — rewrite `_get_vault_info` to read `paths.vault` / `paths.vault_name`.
- `src/juggle_cmd_context.py` — delete `cmd_register_domain` + `cmd_register_domain_path`.
- `src/juggle_cmd_threads.py` — delete `--domain` validation + display in `cmd_create_thread`.
- `src/juggle_cmd_agents.py` — delete `thread_domain` inference block + `domain=` kwarg.
- `commands/start.md` — drop `[--domain D]` from `create-thread` row; add `doctor` row.
- `commands/capture.md` — replace two `domains.initial_domain_paths` snippets.
- `commands/research.md` — replace one `domains.initial_domain_paths` snippet.
- `tests/test_juggle_db_agents.py` — add signature test confirming `domain` is gone.
- `tests/test_data_migration.py` — add Migrations 17–19 assertion test.

**Created:**

- `src/juggle_cmd_doctor.py` — new handler (`cmd_doctor` + pure `_migrate_config`).
- `commands/doctor.md` — new `/juggle:doctor` slash command.
- `tests/test_vault_path_config.py` — covers `_get_vault_root`, `_get_vault_name`, `_get_vault_info`.
- `tests/test_doctor.py` — covers `_migrate_config` cases (full migrate, no-op, preserve existing, missing vault entry).

**Deleted:**

- `tests/test_juggle_domain.py` — every test asserts behavior of removed methods.

---

## Task Ordering Rationale

Task 1 is the high-risk **atomic** change: settings, module-level vars, and migrations move together. If we split it, a partially-applied state crashes `import juggle_db`. Every subsequent task assumes Task 1 is done and committed.

After Task 1, tasks proceed bottom-up: pure DB layer → command modules → CLI wiring → docs → tests. Tests for new surface (vault config helpers, doctor) are TDD-style (red first, then green). Tests for **removed** surface (`test_juggle_domain.py`) are deleted in their consuming task, not earlier — deleting them first would leave the suite green during a partial rewrite.

---

## Task 1: Atomic — Schema migrations, module-level cleanup, settings change

**Files:**
- Modify: `src/juggle_settings.py`
- Modify: `src/juggle_db.py:73-98` (CREATE_DOMAINS / CREATE_DOMAIN_PATHS constants + module-level vars), `src/juggle_db.py:186-209` (`init_db` calls), `src/juggle_db.py:309-327` (Migration 9 body), end of migration block ~line 410 (append Migrations 17–19)

### Step 1.1: Write failing test for Migrations 17–19

- [ ] **Append the new migration test to `tests/test_data_migration.py`**

```python
def test_migration_17_18_19_drops_domain(tmp_path):
    """Migrations 17–19 drop domain columns and tables on an old-schema DB."""
    import sqlite3
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE domains (name TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE domain_paths (path_fragment TEXT PRIMARY KEY, domain TEXT)"
    )
    conn.commit()
    conn.close()

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))
    from juggle_db import JuggleDB
    JuggleDB(db_path).init_db()

    conn2 = sqlite3.connect(db_path)
    cols_threads = {row[1] for row in conn2.execute("PRAGMA table_info(threads)").fetchall()}
    cols_agents = {row[1] for row in conn2.execute("PRAGMA table_info(agents)").fetchall()}
    tables = {row[0] for row in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn2.close()

    assert "domain" not in cols_threads
    assert "domain" not in cols_agents
    assert "domains" not in tables
    assert "domain_paths" not in tables
```

### Step 1.2: Run the test — verify FAIL

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_data_migration.py::test_migration_17_18_19_drops_domain -v`
- Expected: FAIL — Migrations 17–19 don't exist yet, so `domains` table will still be present.

### Step 1.3: Update `src/juggle_settings.py` — DEFAULTS

- [ ] **Open `src/juggle_settings.py`. Inside the `DEFAULTS` dict:**

**Remove** lines 69–77 (the entire `"domains": {...}` block):

```python
"domains": {
    "initial_domains": ["juggle", "vault", "work"],
    "initial_domain_paths": [
        ["/github/juggle", "juggle"],
        ["/Documents/personal", "vault"],
        ["/work/", "work"],
    ],
    "vault_name": "",
},
```

**Replace** the existing `"paths"` block (around lines 44–48) with:

```python
"paths": {
    "data_dir": "~/.claude/juggle",
    "config_dir": "~/.juggle",
    "digest_log_dir": "~/.juggle/logs",
    "vault": "/Documents/personal",
    "vault_name": "",
},
```

Keep any other existing `paths` keys that were already there (e.g., if the file has more than the three originally documented). Add the two new keys; do not delete unrelated keys.

### Step 1.4: Update `src/juggle_db.py` — remove CREATE constants + module-level vars

- [ ] **Delete `src/juggle_db.py` lines 73–98:** the `CREATE_DOMAINS = """..."""` block, the `CREATE_DOMAIN_PATHS = """..."""` block, and the `_INITIAL_DOMAINS` / `_INITIAL_DOMAIN_PATHS` lines. Lines 100+ (`CREATE_NOTIFICATIONS_V2` onward) remain untouched.

- [ ] **Delete `src/juggle_db.py` lines 195 and 196:** the two `conn.execute(CREATE_DOMAINS)` and `conn.execute(CREATE_DOMAIN_PATHS)` calls inside `init_db()`.

### Step 1.5: Update `src/juggle_db.py` — empty Migration 9 body

- [ ] **Replace lines 309–327 with a comment stub:**

```python
        # Migration 9: (removed in 1.16.0) — previously seeded domains/domain_paths
        # tables, which are now dropped in Migrations 17–19. Body intentionally empty.
```

(Keep the indentation matching the surrounding migration blocks — 8 spaces inside the `with self._connect() as conn:` block.)

### Step 1.6: Append Migrations 17–19 to `src/juggle_db.py`

- [ ] **At the end of the migration sequence (after Migration 16, around line 410, inside the same `with self._connect() as conn:` block), append:**

```python
        # Migration 17: drop domain column from threads
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "domain" in cols:
            try:
                domain_indexes = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='index' AND tbl_name='threads' AND sql LIKE '%domain%'"
                    ).fetchall()
                ]
                for idx_name in domain_indexes:
                    conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
                conn.execute("ALTER TABLE threads DROP COLUMN domain")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 17 skipped: %s", e)

        # Migration 18: drop domain column from agents
        agent_cols = {row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
        if "domain" in agent_cols:
            try:
                conn.execute("ALTER TABLE agents DROP COLUMN domain")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 18 skipped: %s", e)

        # Migration 19: drop domain tables (domain_paths FK → domains, drop in order)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "domain_paths" in tables or "domains" in tables:
            try:
                conn.execute("DROP TABLE IF EXISTS domain_paths")
                conn.execute("DROP TABLE IF EXISTS domains")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 19 skipped: %s", e)
```

### Step 1.7: Run the migration test — verify PASS

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_data_migration.py::test_migration_17_18_19_drops_domain -v`
- Expected: PASS.

### Step 1.8: Run the broader migration + db tests as a smoke

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_data_migration.py tests/test_juggle_db.py -v 2>&1 | tail -40`
- Expected: PASS for migration suite. `test_juggle_db.py` may fail on tests that touch domain registry methods — that's OK and gets cleaned up in Task 3. Note the failures but proceed.

### Step 1.9: Commit

- [ ] ```bash
git add src/juggle_settings.py src/juggle_db.py tests/test_data_migration.py
git commit -m "refactor: drop domain machinery (schema + settings) and add Migrations 17–19

Removes DEFAULTS['domains'], _INITIAL_DOMAINS/_INITIAL_DOMAIN_PATHS module
vars, CREATE_DOMAINS/CREATE_DOMAIN_PATHS constants, and Migration 9's body
in one atomic change to avoid an import-time KeyError. Adds Migrations
17–19 dropping threads.domain, agents.domain, and the domains/domain_paths
tables. Towards 1.16.0."
```

---

## Task 2: Simplify `get_best_agent`

**Files:**
- Modify: `src/juggle_db.py:980-1024`

### Step 2.1: Write failing test for the new signature

- [ ] **Append to `tests/test_juggle_db_agents.py`:**

```python
def test_get_best_agent_signature_has_no_domain():
    """get_best_agent must not accept a domain kwarg after 1.16.0 cleanup."""
    import inspect
    from juggle_db import JuggleDB
    sig = inspect.signature(JuggleDB.get_best_agent)
    assert "domain" not in sig.parameters
```

### Step 2.2: Run — verify FAIL

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_juggle_db_agents.py::test_get_best_agent_signature_has_no_domain -v`
- Expected: FAIL — current signature still has `domain`.

### Step 2.3: Rewrite `get_best_agent`

- [ ] **In `src/juggle_db.py` (around lines 980–1024), replace the existing `get_best_agent` with:**

```python
    def get_best_agent(self, thread_id: str, role: str | None = None) -> dict | None:
        idle = [a for a in self.get_all_agents() if a["status"] == "idle"]
        if not idle:
            return None

        def _score(agent: dict) -> tuple:
            context = json.loads(agent.get("context_threads") or "[]")
            s = 0
            if agent.get("assigned_thread") == thread_id:
                s += 3
            if thread_id in context:
                s += 2
            if role and agent["role"] == role:
                s += 1
            return (s, agent["last_active"])

        return max(idle, key=_score)
```

### Step 2.4: Run — verify PASS

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_juggle_db_agents.py -v 2>&1 | tail -20`
- Expected: New signature test PASSES. Other `test_juggle_db_agents.py` tests should also pass since they already omit the `domain` kwarg (per spec audit).

### Step 2.5: Commit

- [ ] ```bash
git add src/juggle_db.py tests/test_juggle_db_agents.py
git commit -m "refactor: simplify get_best_agent — drop domain filter (always no-op in prod)"
```

---

## Task 3: Remove domain registry methods from `juggle_db`

**Files:**
- Modify: `src/juggle_db.py:1030-1080` (delete `register_domain`, `get_domains`, `is_known_domain`, `add_domain_path`, `get_domain_paths`, `infer_domain_from_prompt`)

### Step 3.1: Delete the six methods

- [ ] **In `src/juggle_db.py`, delete the methods at the following starting lines:**
  - `register_domain` (line ~1030)
  - `get_domains` (line ~1036)
  - `is_known_domain` (line ~1042)
  - `add_domain_path` (line ~1050)
  - `get_domain_paths` (line ~1059)
  - `infer_domain_from_prompt` (line ~1067)

Each method is a small block; delete each method definition through to (but not including) the next `def ` line. Use `grep -n "def register_domain\|def get_domains\|def is_known_domain\|def add_domain_path\|def get_domain_paths\|def infer_domain_from_prompt\|def " src/juggle_db.py` to confirm exact ranges before editing.

### Step 3.2: Run smoke — `juggle_db` must still import

- [ ] Run: `cd ~/github/juggle && python3 -c "from juggle_db import JuggleDB; print('ok')"`
- Expected: `ok`.

### Step 3.3: Run db-touching tests

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_juggle_db.py tests/test_juggle_db_agents.py tests/test_juggle_db_memory.py -v 2>&1 | tail -30`
- Expected: any test that called the removed methods now fails. **These should only live in `tests/test_juggle_domain.py`** (deleted in Task 9). If any failure surfaces in another file, stop and investigate before continuing — it means a non-`test_juggle_domain.py` callsite exists that the spec audit missed.

### Step 3.4: Commit

- [ ] ```bash
git add src/juggle_db.py
git commit -m "refactor: remove dead domain registry methods from JuggleDB

Removes register_domain, get_domains, is_known_domain, add_domain_path,
get_domain_paths, infer_domain_from_prompt — none have live callers
after the get_best_agent simplification."
```

---

## Task 4: Update `cmd_get_agent` in `juggle_cmd_agents.py`

**Files:**
- Modify: `src/juggle_cmd_agents.py:413-430`

### Step 4.1: Apply the edit

- [ ] **In `src/juggle_cmd_agents.py`, replace lines 413–430 (the `thread_domain` inference block and the surrounding `update_agent` call):**

**Before:**

```python
    thread_domain = thread.get("domain") if thread else None
    if thread_domain is None:
        thread_domain = db.infer_domain_from_prompt(thread.get("topic", "") if thread else "")

    agent = db.get_best_agent(thread_uuid, role=args.role, domain=thread_domain)
    is_new = agent is None

    if is_new:
        ...

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent["id"], status="busy", assigned_thread=thread_uuid,
                    last_active=now, domain=thread_domain)
```

**After:**

```python
    agent = db.get_best_agent(thread_uuid, role=args.role)
    is_new = agent is None

    if is_new:
        ...

    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent["id"], status="busy", assigned_thread=thread_uuid,
                    last_active=now)
```

(Note: the `if is_new:` body — the `...` — is the existing branch; leave it intact.)

### Step 4.2: Confirm no other `thread_domain` / `domain=` references remain

- [ ] Run: `grep -n "thread_domain\|domain=" src/juggle_cmd_agents.py`
- Expected: zero hits. If any remain, edit them out.

### Step 4.3: Run the agent command tests

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_juggle_db_agents.py tests/test_juggle_cli.py -v 2>&1 | tail -20`
- Expected: PASS.

### Step 4.4: Commit

- [ ] ```bash
git add src/juggle_cmd_agents.py
git commit -m "refactor(agents): drop thread_domain inference + update_agent domain kwarg"
```

---

## Task 5: Update `cmd_create_thread` in `juggle_cmd_threads.py`

**Files:**
- Modify: `src/juggle_cmd_threads.py:104-113`

### Step 5.1: Apply the edit

- [ ] **In `src/juggle_cmd_threads.py:cmd_create_thread`, replace lines 104–113.**

**Before:**

```python
    domain = getattr(args, "domain", None)
    if domain is not None and not db.is_known_domain(domain):
        print(f"Unknown domain '{domain}'. Run: juggle register-domain {domain}")
        return 1
    thread_uuid = db.create_thread(args.topic, session_id="", domain=domain)
    label = db.get_thread(thread_uuid).get("user_label")
    db.set_active_thread(thread_uuid)
    domain_str = f" [domain={domain}]" if domain else ""
    print(f"Created Topic {label}: {args.topic}.{domain_str} Now in Topic {label}.")
```

**After:**

```python
    thread_uuid = db.create_thread(args.topic, session_id="")
    label = db.get_thread(thread_uuid).get("user_label")
    db.set_active_thread(thread_uuid)
    print(f"Created Topic {label}: {args.topic}. Now in Topic {label}.")
```

### Step 5.2: Verify `db.create_thread` no longer accepts `domain`

- [ ] Run: `grep -n "def create_thread" src/juggle_db.py`
- Inspect the signature. If `create_thread(self, topic, ..., domain=...)` still has `domain` as a parameter, remove that parameter and any usage inside the body. (This isn't called out as a separate task because it's a one-line tweak inside the existing method.)
- Run: `grep -n "domain" src/juggle_db.py` and confirm no remaining live references (commented references are fine).

### Step 5.3: Run thread tests

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_juggle_cli.py tests/test_thread_state_machine.py -v 2>&1 | tail -20`
- Expected: PASS.

### Step 5.4: Commit

- [ ] ```bash
git add src/juggle_cmd_threads.py src/juggle_db.py
git commit -m "refactor(threads): drop --domain validation and create_thread domain kwarg"
```

---

## Task 6: Remove domain handler funcs from `juggle_cmd_context.py`

**Files:**
- Modify: `src/juggle_cmd_context.py:119-131`

### Step 6.1: Delete handlers

- [ ] **In `src/juggle_cmd_context.py`, delete the entire `cmd_register_domain` function (around line 119) and `cmd_register_domain_path` function (around line 125). Also fix the module docstring on line 2 — remove the word `domain` from `"""Juggle CLI — Shared context, memory, domain, and misc commands."""`.**

Confirm:

- [ ] Run: `grep -n "def cmd_register_domain\|def cmd_register_domain_path" src/juggle_cmd_context.py`
- Expected: no matches.

### Step 6.2: Run quick smoke

- [ ] Run: `cd ~/github/juggle && python3 -c "from juggle_cmd_context import *; print('ok')"`
- Expected: `ok`.

### Step 6.3: Commit

- [ ] ```bash
git add src/juggle_cmd_context.py
git commit -m "refactor(context): drop cmd_register_domain + cmd_register_domain_path handlers"
```

---

## Task 7: Rewrite vault helpers and remove old subparsers in `juggle_cli.py`

**Files:**
- Modify: `src/juggle_cli.py:41-57` (vault helpers), `src/juggle_cli.py:108-109` (imports), `src/juggle_cli.py:214-231` (subparsers + dispatch)

### Step 7.1: Write failing tests for the new vault helpers

- [ ] **Create `tests/test_vault_path_config.py`:**

```python
"""Tests for paths.vault + paths.vault_name config reading."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_get_vault_root_from_paths_vault():
    """_get_vault_root() reads paths.vault from settings."""
    with patch("juggle_cli.get_settings", return_value={
        "paths": {"vault": "/Documents/test-vault", "vault_name": ""},
    }):
        from juggle_cli import _get_vault_root
        root = _get_vault_root()
        assert root == Path.home() / "Documents/test-vault"


def test_get_vault_name_explicit():
    """_get_vault_name() returns explicit vault_name when set."""
    with patch("juggle_cli.get_settings", return_value={
        "paths": {"vault": "/Documents/personal", "vault_name": "MyVault"},
    }):
        from juggle_cli import _get_vault_name
        assert _get_vault_name() == "MyVault"


def test_get_vault_name_derived_from_path():
    """_get_vault_name() derives name from vault path when vault_name is empty."""
    with patch("juggle_cli.get_settings", return_value={
        "paths": {"vault": "/Documents/personal", "vault_name": ""},
    }):
        from juggle_cli import _get_vault_name
        assert _get_vault_name() == "personal"


def test_get_vault_info_research():
    """_get_vault_info() in juggle_cmd_research reads paths.vault."""
    with patch("juggle_cmd_research.get_settings", return_value={
        "paths": {"vault": "/Documents/personal", "vault_name": "personal"},
    }):
        from juggle_cmd_research import _get_vault_info
        vault_path, vault_name = _get_vault_info()
        assert vault_path.endswith("/Documents/personal")
        assert vault_name == "personal"
```

### Step 7.2: Run — verify FAIL

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_vault_path_config.py -v`
- Expected: the first three tests FAIL (current helpers still read from `domains`). The `test_get_vault_info_research` test will be addressed in Task 8 and may also fail here.

### Step 7.3: Rewrite `_get_vault_root` and `_get_vault_name`

- [ ] **Replace `src/juggle_cli.py` lines 41–55 (the two helper functions) with:**

```python
def _get_vault_root() -> Path:
    from juggle_settings import get_settings
    vault_rel = get_settings()["paths"].get("vault", "/Documents/personal")
    return Path.home() / vault_rel.lstrip("/")


def _get_vault_name() -> str:
    from juggle_settings import get_settings
    explicit = get_settings()["paths"].get("vault_name", "")
    if explicit:
        return explicit
    return _get_vault_root().name
```

Leave `VAULT_ROOT = _get_vault_root()` on line 57 in place (it's already module-level eager evaluation).

### Step 7.4: Remove the `register-domain` / `--domain` CLI surface

- [ ] **In `src/juggle_cli.py`:**

(a) Remove the `--domain` argument on the `create-thread` subparser (lines 217–218):

```python
    p_create.add_argument("--domain", dest="domain", default=None,
                          help="Domain (must be registered).")
```

Delete both lines.

(b) Remove the `register-domain` subparser block (lines 222–225) and the `register-domain-path` subparser block (lines 227–231).

(c) Remove the now-unused imports for `cmd_register_domain` and `cmd_register_domain_path` from the imports section near lines 108–109. Use `grep -n "cmd_register_domain" src/juggle_cli.py` to confirm both imports and any dispatch sites are gone.

(d) Search for and delete the dispatch branches in the `if args.cmd == "..."` chain:

```python
    elif args.cmd == "register-domain":
        cmd_register_domain(args)
    elif args.cmd == "register-domain-path":
        cmd_register_domain_path(args)
```

### Step 7.5: Run vault helper tests — verify PASS

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_vault_path_config.py::test_get_vault_root_from_paths_vault tests/test_vault_path_config.py::test_get_vault_name_explicit tests/test_vault_path_config.py::test_get_vault_name_derived_from_path -v`
- Expected: 3 PASS.

### Step 7.6: Smoke — CLI still parses

- [ ] Run: `cd ~/github/juggle && python3 src/juggle_cli.py --help 2>&1 | tail -30`
- Expected: help text prints without exception, no `register-domain` subcommand listed.

### Step 7.7: Commit

- [ ] ```bash
git add src/juggle_cli.py tests/test_vault_path_config.py
git commit -m "refactor(cli): vault helpers read paths.vault; drop register-domain CLI

_get_vault_root and _get_vault_name now read paths.vault and
paths.vault_name from settings. Removes --domain arg from create-thread
and both register-domain subcommands."
```

---

## Task 8: Update `_get_vault_info` in `juggle_cmd_research.py`

**Files:**
- Modify: `src/juggle_cmd_research.py` (function `_get_vault_info`)

### Step 8.1: Rewrite the function

- [ ] **Locate `_get_vault_info()` in `src/juggle_cmd_research.py` (`grep -n "_get_vault_info" src/juggle_cmd_research.py` to find it). Replace its body with:**

```python
def _get_vault_info() -> tuple[str, str]:
    from juggle_settings import get_settings
    s = get_settings()
    vault_rel = s["paths"].get("vault", "/Documents/personal")
    vault_path = str(Path.home() / vault_rel.lstrip("/"))
    explicit_name = s["paths"].get("vault_name", "")
    vault_name = explicit_name if explicit_name else Path(vault_path).name
    return vault_path, vault_name
```

Ensure `from pathlib import Path` is imported at module top (it almost certainly already is; confirm with `grep -n "^from pathlib\|^import pathlib" src/juggle_cmd_research.py`).

### Step 8.2: Run the research vault-info test — verify PASS

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_vault_path_config.py::test_get_vault_info_research -v`
- Expected: PASS.

### Step 8.3: Commit

- [ ] ```bash
git add src/juggle_cmd_research.py
git commit -m "refactor(research): _get_vault_info reads paths.vault"
```

---

## Task 9: Delete `tests/test_juggle_domain.py`

**Files:**
- Delete: `tests/test_juggle_domain.py`

### Step 9.1: Delete the file

- [ ] Run: `rm ~/github/juggle/tests/test_juggle_domain.py`

### Step 9.2: Run the full test suite as a smoke

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/ -x 2>&1 | tail -30`
- Expected: zero failures attributable to domain removal. If failures appear, they should be in tests that reference removed surface — fix them per the failure mode (likely deletion). Stop and investigate any unexpected failures.

### Step 9.3: Commit

- [ ] ```bash
git add -A tests/test_juggle_domain.py
git commit -m "test: delete test_juggle_domain.py — all surface removed in 1.16.0"
```

---

## Task 10: Add `juggle_cmd_doctor.py` with `_migrate_config`

**Files:**
- Create: `src/juggle_cmd_doctor.py`
- Create: `tests/test_doctor.py`

### Step 10.1: Write failing tests for `_migrate_config`

- [ ] **Create `tests/test_doctor.py`:**

```python
"""Tests for juggle doctor config migration helper."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_cmd_doctor import _migrate_config  # noqa: E402


def test_migrate_config_moves_vault_path():
    cfg = {
        "paths": {"data_dir": "~/.claude/juggle"},
        "domains": {
            "initial_domains": ["juggle", "vault"],
            "initial_domain_paths": [
                ["/github/juggle", "juggle"],
                ["/Documents/my-vault", "vault"],
            ],
            "vault_name": "MyVault",
        },
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg["paths"]["vault"] == "/Documents/my-vault"
    assert new_cfg["paths"]["vault_name"] == "MyVault"
    assert "domains" not in new_cfg
    assert len(changes) >= 2


def test_migrate_config_preserves_existing_paths_vault():
    """If user has already set paths.vault, do not overwrite it."""
    cfg = {
        "paths": {"vault": "/Documents/already-set"},
        "domains": {
            "initial_domain_paths": [["/Documents/should-not-use", "vault"]],
            "vault_name": "ShouldNotUse",
        },
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg["paths"]["vault"] == "/Documents/already-set"
    assert "domains" not in new_cfg


def test_migrate_config_no_op_when_no_domains_block():
    cfg = {"paths": {"vault": "/Documents/personal"}}
    new_cfg, changes = _migrate_config(dict(cfg))
    assert new_cfg == cfg
    assert changes == []


def test_migrate_config_handles_missing_vault_entry():
    """domains block without a 'vault' path: still strip block, leave paths alone."""
    cfg = {
        "paths": {},
        "domains": {"initial_domain_paths": [["/github/juggle", "juggle"]]},
    }
    new_cfg, changes = _migrate_config(dict(cfg))
    assert "domains" not in new_cfg
    assert "vault" not in new_cfg["paths"]
```

### Step 10.2: Run — verify FAIL

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_doctor.py -v`
- Expected: FAIL on import (file `juggle_cmd_doctor.py` doesn't exist yet).

### Step 10.3: Create `src/juggle_cmd_doctor.py`

- [ ] **Write the new file:**

```python
"""Juggle CLI — `doctor` subcommand: migrate config + DB to current schema."""

import json
import shutil
import sqlite3
import sys
from pathlib import Path


CONFIG_PATH = Path.home() / ".juggle" / "config.json"
BACKUP_PATH = Path.home() / ".juggle" / "config.json.bak-pre-1.16"


def _migrate_config(cfg: dict) -> tuple[dict, list[str]]:
    """Pure helper: rewrite a config dict from the pre-1.16 schema to 1.16+.

    Returns (new_cfg, list_of_change_descriptions).
    """
    changes: list[str] = []
    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        return cfg, changes

    paths = cfg.setdefault("paths", {})

    if "vault" not in paths:
        initial_paths = domains.get("initial_domain_paths") or []
        for entry in initial_paths:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) >= 2
                and entry[1] == "vault"
            ):
                paths["vault"] = entry[0]
                changes.append(
                    f"paths.vault = {entry[0]} (migrated from domains.initial_domain_paths)"
                )
                break

    if "vault_name" not in paths:
        legacy_name = domains.get("vault_name", "")
        if legacy_name:
            paths["vault_name"] = legacy_name
            changes.append(
                f"paths.vault_name = {legacy_name} (migrated from domains.vault_name)"
            )

    del cfg["domains"]
    changes.append("removed obsolete domains block")
    return cfg, changes


def cmd_doctor(args) -> int:
    dry = getattr(args, "dry_run", False)
    print(f"juggle doctor — dry_run={dry}")

    # 1. Config
    if CONFIG_PATH.exists():
        original = json.loads(CONFIG_PATH.read_text())
        new_cfg, changes = _migrate_config(dict(original))
        if changes:
            if not dry:
                if not BACKUP_PATH.exists():
                    shutil.copy2(CONFIG_PATH, BACKUP_PATH)
                CONFIG_PATH.write_text(json.dumps(new_cfg, indent=2))
            print(f"config: {len(changes)} change(s):")
            for c in changes:
                print(f"  - {c}")
            print(f"  backup: {BACKUP_PATH}" if not dry else "  (dry-run — no write)")
        else:
            print("config: already on 1.16.0 schema")
    else:
        print(f"config: {CONFIG_PATH} does not exist — nothing to migrate")

    # 2. DB (presence-based; juggle has no schema_version table)
    from juggle_db import JuggleDB, DB_PATH

    if Path(DB_PATH).exists():
        conn = sqlite3.connect(str(DB_PATH))
        thread_cols = {row[1] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        stale = (
            "domain" in thread_cols
            or "domains" in tables
            or "domain_paths" in tables
        )
        if stale:
            if not dry:
                JuggleDB(DB_PATH).init_db()
                print("db: ran Migrations 17–19 (dropped domain column + domain tables)")
            else:
                print("db: would run Migrations 17–19 (stale schema detected)")
        else:
            print("db: schema already on 1.16.0")
    else:
        print(f"db: {DB_PATH} does not exist — will be created on first juggle command")

    return 0
```

### Step 10.4: Run — verify PASS

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/test_doctor.py -v`
- Expected: 4 PASS.

### Step 10.5: Commit

- [ ] ```bash
git add src/juggle_cmd_doctor.py tests/test_doctor.py
git commit -m "feat(doctor): add juggle_cmd_doctor.py with _migrate_config helper

Pure _migrate_config function rewrites old domains.initial_domain_paths
to paths.vault / paths.vault_name. cmd_doctor wraps it with backup,
write, and DB schema check."
```

---

## Task 11: Wire `doctor` into `juggle_cli.py`

**Files:**
- Modify: `src/juggle_cli.py` (subparser + dispatch)

### Step 11.1: Add subparser registration

- [ ] **In `src/juggle_cli.py`, near the other `subparsers.add_parser(...)` calls (after `create-thread`), add:**

```python
    # doctor — auto-migrate config + DB to current schema
    p_doctor = subparsers.add_parser("doctor", help="Migrate config + DB to current schema")
    p_doctor.add_argument("--dry-run", action="store_true",
                          help="Print actions; write nothing")
```

### Step 11.2: Add dispatch branch

- [ ] **In the `if args.cmd == "..."` dispatch chain in `juggle_cli.py:main()`, add:**

```python
    elif args.cmd == "doctor":
        from juggle_cmd_doctor import cmd_doctor
        sys.exit(cmd_doctor(args))
```

### Step 11.3: Smoke — `juggle doctor --dry-run`

- [ ] Run: `cd ~/github/juggle && python3 src/juggle_cli.py doctor --dry-run`
- Expected: prints `juggle doctor — dry_run=True`, then reports on config + DB. Exit code 0.

### Step 11.4: Commit

- [ ] ```bash
git add src/juggle_cli.py
git commit -m "feat(cli): wire 'juggle doctor' subcommand"
```

---

## Task 12: Update slash-command markdown — `start.md`, `capture.md`, `research.md`

**Files:**
- Modify: `commands/start.md`, `commands/capture.md`, `commands/research.md`

### Step 12.1: `commands/start.md`

- [ ] **Open `commands/start.md`. Find the CLI reference table row for `create-thread`:**

```
| `create-thread` | `<label> [--domain D]` | New topic |
```

Replace with:

```
| `create-thread` | `<label>` | New topic |
| `doctor`        | `[--dry-run]` | Migrate config + DB to current schema |
```

### Step 12.2: `commands/capture.md` — VAULT_PATH snippet

- [ ] **Find the Python block that contains:**

```python
paths = s['domains']['initial_domain_paths']
vault_rel = next((p[0] for p in paths if p[1] == 'vault'), '/Documents/personal')
print(os.path.expanduser('~') + vault_rel)
```

Replace those three lines with:

```python
vault_rel = s['paths'].get('vault', '/Documents/personal')
print(os.path.expanduser('~') + vault_rel)
```

### Step 12.3: `commands/capture.md` — VAULT_NAME snippet

- [ ] **Find the Python block that contains:**

```python
explicit = s['domains'].get('vault_name', '')
if explicit:
    print(explicit)
else:
    paths = s['domains']['initial_domain_paths']
    vault_rel = next((p[0] for p in paths if p[1] == 'vault'), '/Documents/personal')
    print(Path(vault_rel.rstrip('/')).name)
```

Replace with:

```python
explicit = s['paths'].get('vault_name', '')
if explicit:
    print(explicit)
else:
    vault_rel = s['paths'].get('vault', '/Documents/personal')
    print(Path(vault_rel.rstrip('/')).name)
```

### Step 12.4: `commands/research.md` — vault snippet in Step 5

- [ ] **Find:**

```python
paths = get_settings()['domains']['initial_domain_paths']
vault = next((p[0] for p in paths if p[1] == 'vault'), None)
print(os.path.expanduser('~') + vault if vault else '')
```

Replace with:

```python
vault_rel = get_settings()['paths'].get('vault', '')
print(os.path.expanduser('~') + vault_rel if vault_rel else '')
```

### Step 12.5: Spot-check — no stale `domains` references in commands

- [ ] Run: `grep -rn "initial_domain_paths\|s\\['domains'\\]\\|domains\\." commands/ | grep -v "doctor.md"`
- Expected: no live references to the old config keys (the `commands/doctor.md` file in Task 13 will mention `domains` in human-readable text — that's fine).

### Step 12.6: Commit

- [ ] ```bash
git add commands/start.md commands/capture.md commands/research.md
git commit -m "docs(commands): update start/capture/research to use paths.vault"
```

---

## Task 13: Create `/juggle:doctor` slash command

**Files:**
- Create: `commands/doctor.md`

### Step 13.1: Write the slash command

- [ ] **Create `commands/doctor.md`:**

````markdown
---
description: Migrate Juggle config + DB to the current schema (one-shot upgrade helper).
allowed-tools: Bash
---

# /juggle:doctor — Config + DB Migration

Runs the `juggle doctor` CLI which:

1. Backs up `~/.juggle/config.json` to `~/.juggle/config.json.bak-pre-1.16`.
2. Rewrites the config to move `domains.initial_domain_paths` (vault entry) into `paths.vault`, and `domains.vault_name` into `paths.vault_name`.
3. Removes the obsolete `domains` block.
4. Runs Migrations 17–19 on `~/.claude/juggle/juggle.db`, dropping `threads.domain`, `agents.domain`, and the `domains` / `domain_paths` tables.

## Run

```bash
python3 ~/github/juggle/src/juggle_cli.py doctor
```

For a preview without writes:

```bash
python3 ~/github/juggle/src/juggle_cli.py doctor --dry-run
```

Report the output. If the user wants to revert, restore `~/.juggle/config.json.bak-pre-1.16` and downgrade Juggle to a 1.15.x release.
````

### Step 13.2: Commit

- [ ] ```bash
git add commands/doctor.md
git commit -m "feat(commands): add /juggle:doctor slash command"
```

---

## Task 14: Final integration check + version bump

**Files:**
- Modify: wherever the version string lives (run `grep -rn "1\\.15" src/ commands/ 2>/dev/null | head` to confirm — if there's no version file, this step is a no-op).

### Step 14.1: Run the full test suite

- [ ] Run: `cd ~/github/juggle && python3 -m pytest tests/ -v 2>&1 | tail -50`
- Expected: full PASS. Any FAIL must be triaged before declaring complete.

### Step 14.2: Manual end-to-end smoke

- [ ] Run: `cd ~/github/juggle && python3 src/juggle_cli.py doctor`
- Expected: reports config migration (likely already-on-1.16 if the live config was already in shape) and DB migration. Exit 0.

- [ ] Run: `python3 src/juggle_cli.py --help`
- Expected: help text shows `doctor` subcommand; no `register-domain` or `register-domain-path`.

- [ ] Run: `python3 src/juggle_cli.py create-thread --help`
- Expected: no `--domain` arg.

### Step 14.3: Bump version

- [ ] Run: `grep -rn "1\\.15\\.\\|VERSION\\s*=\\|__version__" src/ 2>/dev/null`
- If a version string exists, bump it from `1.15.x` to `1.16.0` and stage the change.
- If no version string exists in source, skip — this repo tracks version elsewhere.

### Step 14.4: Final commit (if version changed)

- [ ] ```bash
git add -A
git commit -m "chore: bump to 1.16.0 — domain cleanup + vault-path refactor

Removes the legacy `domains` config block and the `register-domain` CLI
surface; vault path now lives at paths.vault / paths.vault_name. Adds
/juggle:doctor for one-shot config + DB migration.

Co-author note: hard cutover, no fallback. Custom vault paths require
running /juggle:doctor or hand-editing config.json."
```

---

## Self-Review Checklist

After all tasks are complete:

- [ ] **Spec coverage** — every Section 1 file in the spec maps to a task above. Section 6 `/juggle:doctor` covered by Tasks 10–11–13. Section 7 docs covered by Tasks 12–13. Section 8 tests covered by Tasks 1, 2, 7, 10, 9.
- [ ] **Placeholder scan** — `grep -n "TBD\|TODO\|XXX\|FIXME" plan/2026-05-17-domain-cleanup-vault-path-refactor-plan.md` returns nothing for this plan file's *content* (matches inside code-block strings of the live tests are fine; the plan does not introduce TODOs of its own).
- [ ] **Type consistency** — `_migrate_config` signature `(cfg: dict) -> tuple[dict, list[str]]` is consistent between spec, plan, and tests. `cmd_doctor(args) -> int` consistent. `_get_vault_root() -> Path` consistent. `_get_vault_name() -> str` consistent. `_get_vault_info() -> tuple[str, str]` consistent. `get_best_agent(self, thread_id, role=None) -> dict | None` consistent.
- [ ] **Subagent prompt boilerplate** — when dispatching the coder agent, include the standard pre-PR gate: "Before calling `complete-agent`, invoke `mike:pre-pr` skill to run the quality gate. Do NOT open a PR." Commit-to-main policy applies per Juggle project rule; no PR.

---

## Out of Scope (Explicit)

These items are spec'd in the future-compatibility note (Section 9) but are NOT part of this plan:

- Adding `threads.project` column (Migration 20) — deferred.
- Wiring `--project` CLI flag to a real DB column — deferred until Migration 20.
- Deleting Migration 9 entirely (vs. emptying its body) — emptying is the chosen path; full deletion is rejected as a destabilizing churn.
