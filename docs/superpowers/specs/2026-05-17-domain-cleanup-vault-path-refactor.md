# Domain Cleanup + Vault-Path Refactor — Spec

**Status:** Implemented 2026-05-17 (v1.21.0)
**Date:** 2026-05-17
**Version bump target:** 1.15.0 → 1.16.0 (minor — feature removal + config surface change)

---

## Summary

Two coordinated changes plus a migration helper:

**(a) Remove dead `domain` machinery** — `threads.domain`, `agents.domain` columns; `domains` and `domain_paths` tables; `register-domain` / `register-domain-path` CLI; `get_best_agent` domain-filter branch; all related DB methods.

**(b) Vault-path config refactor** — vault path and name lookups currently piggyback on `domains.initial_domain_paths`. Move them to their own `paths.vault` + `paths.vault_name` keys inside the existing `paths` config block.

**(c) `/juggle:doctor` auto-migration command** — one-shot tool that rewrites old `~/.juggle/config.json` (`domains` → `paths.vault`/`paths.vault_name`) and confirms the DB has run Migrations 17–19. Replaces the deprecation fallback we initially planned.

**Constraint:** Do NOT add a `project` column to `threads` yet. Design migrations so a future `ALTER TABLE threads ADD COLUMN project TEXT` (Migration 20) is a clean, low-risk follow-up.

**Cutover model:** Hard cutover. No silent-fallback code path. Users with non-default vault paths must either edit `config.json` by hand or run `/juggle:doctor`. User base is small enough (per Mike) that the breaking change is acceptable in a single version bump.

---

## 1. Files Changed

| File                              | Change                                                                                                                                                                                                                                                                                                                                                                                                                            | Risk                                              |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| `src/juggle_settings.py`          | Add `paths.vault` + `paths.vault_name` to `DEFAULTS["paths"]`; remove entire `DEFAULTS["domains"]` block                                                                                                                                                                                                                                                                                                                          | Medium — config read-path changes for all callers |
| `src/juggle_db.py`                | Remove `CREATE_DOMAINS`, `CREATE_DOMAIN_PATHS` constants and their `init_db()` calls; remove `_INITIAL_DOMAINS`, `_INITIAL_DOMAIN_PATHS` module vars (lines 95–98); empty Migration 9's body (keep comment+number for sequence integrity); add Migrations 17–19; remove domain registry methods (`register_domain`, `get_domains`, `is_known_domain`, `add_domain_path`, `get_domain_paths`, `infer_domain_from_prompt`); simplify `get_best_agent` (remove `domain` param + filter) | Medium — schema migration; method API change      |
| `src/juggle_cli.py`               | Rewrite `_get_vault_root()` and `_get_vault_name()` to read `paths.vault` / `paths.vault_name` (no fallback)                                                                                                                                                                                                                                                                                                                      | Low                                               |
| `src/juggle_cmd_research.py`      | Rewrite `_get_vault_info()` to read `paths.vault` / `paths.vault_name`                                                                                                                                                                                                                                                                                                                                                            | Low                                               |
| `src/juggle_cmd_context.py`       | Remove `cmd_register_domain` and `cmd_register_domain_path` functions                                                                                                                                                                                                                                                                                                                                                             | Low                                               |
| `src/juggle_cmd_threads.py`       | Remove `--domain` validation and `domain` arg from `cmd_create_thread`                                                                                                                                                                                                                                                                                                                                                            | Low                                               |
| `src/juggle_cmd_agents.py`        | Remove `thread_domain` inference block in `cmd_get_agent`; remove `domain=thread_domain` from `update_agent` call                                                                                                                                                                                                                                                                                                                 | Low                                               |
| `src/juggle_cmd_doctor.py`        | **New file** — `cmd_doctor(args)` implements config + DB migration check                                                                                                                                                                                                                                                                                                                                                          | Low                                               |
| `commands/doctor.md`              | **New file** — `/juggle:doctor` Claude slash command, dispatches `juggle doctor` and reports results                                                                                                                                                                                                                                                                                                                              | Low                                               |
| `commands/start.md`               | Remove `[--domain D]` from `create-thread` row in CLI reference table; add `doctor` row                                                                                                                                                                                                                                                                                                                                           | Low                                               |
| `commands/capture.md`             | Replace both inline Python vault-resolution snippets                                                                                                                                                                                                                                                                                                                                                                              | Low                                               |
| `commands/research.md`            | Replace inline Python vault-path snippet                                                                                                                                                                                                                                                                                                                                                                                          | Low                                               |
| `tests/test_juggle_domain.py`     | **Delete entire file** — all tests become invalid after domain removal                                                                                                                                                                                                                                                                                                                                                            | Low                                               |
| `tests/test_juggle_db_agents.py`  | Verify no `domain=` kwargs remain; add a `get_best_agent`-signature test                                                                                                                                                                                                                                                                                                                                                          | Low                                               |
| `tests/test_vault_path_config.py` | **New file** — tests for `paths.vault` / `paths.vault_name` config reading                                                                                                                                                                                                                                                                                                                                                        | Low                                               |
| `tests/test_doctor.py`            | **New file** — tests for `cmd_doctor` config migration (old → new) and idempotency                                                                                                                                                                                                                                                                                                                                                | Low                                               |
| `tests/test_data_migration.py`    | Add Migrations 17–19 schema-drop assertions                                                                                                                                                                                                                                                                                                                                                                                       | Low                                               |

---

## 2. DB Migration

### New migrations (append after Migration 16)

```python
# Migration 17: drop domain column from threads
cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
if "domain" in cols:
    try:
        # Pre-check: drop any index that mentions the domain column
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

### SQLite version constraint

`ALTER TABLE DROP COLUMN` requires **SQLite ≥ 3.35.0** (2021-03-12).
Python 3.11 ships SQLite 3.39.x; Python 3.12+ ships 3.45.x. Juggle requires Python 3.11+, so this is safe on all supported platforms.

If the pre-check finds an index on `threads.domain`, it drops it first — SQLite rejects `DROP COLUMN` on indexed columns. No such index is expected (the column was never queried), but the guard is cheap and avoids a silent skip.

### Rollback

SQLite has no `ALTER TABLE ADD COLUMN ... AFTER` or native migration rollback. **Before deploying, take a DB backup:**

```bash
cp ~/.claude/juggle/juggle.db ~/.claude/juggle/juggle.db.bak-pre-1.16
```

To roll back: restore the backup. The code change is required to accompany the backup restore (downgrade to 1.15.x or revert the settings change). `/juggle:doctor` also writes a `~/.juggle/config.json.bak-pre-1.16` before rewriting config.

### Migration 9 — replace body with a no-op comment

Migration 9 seeds `domains` and `domain_paths` tables. After Migrations 17–19 these tables are gone, and `DEFAULTS["domains"]` is gone too, so the seed-from-settings logic can no longer execute. Juggle's migration framework is presence-based (each migration guards itself with `if "<col>" not in cols:` or `if "<table>" in tables:`) — there is no `schema_version` table to bump. Resolution (Mike: "whatever is easier and better for long-term code health"):

- **Replace Migration 9's body (lines 309–327) with a single comment block.** Leave the `# Migration 9: ...` header so the numbering between Migration 8 and Migration 10 stays readable.
- Don't delete Migration 9 outright — that would just move every downstream migration up a line and make `git blame` noisier without buying anything.

```python
# Migration 9: (removed in 1.16.0) — previously seeded domains/domain_paths
# tables, which are now dropped in Migrations 17–19. Body intentionally empty.
```

---

## 3. CLI Surface — Removed + Added

### Removed commands

| Command                                         | Subparser location | Handler location                                 |
| ----------------------------------------------- | ------------------ | ------------------------------------------------ |
| `register-domain <name>`                        | `juggle_cli.py`    | `juggle_cmd_context.py:cmd_register_domain`      |
| `register-domain-path <path_fragment> <domain>` | `juggle_cli.py`    | `juggle_cmd_context.py:cmd_register_domain_path` |

Remove both subparser registrations from `juggle_cli.py` (lines ~222–231) and both handler functions from `juggle_cmd_context.py` (lines ~119–131). Remove the corresponding imports in `juggle_cli.py` (lines ~108–109).

### Removed flag

Remove `--domain` argument from the `create-thread` subparser in `juggle_cli.py` (line ~217–218). In `juggle_cmd_threads.py:cmd_create_thread`, remove the `getattr(args, "domain", None)` read, the `is_known_domain` validation, and the `domain_str` display suffix (lines ~104–113).

### New command: `juggle doctor`

| Command   | Args                  | Handler                          |
| --------- | --------------------- | -------------------------------- |
| `doctor`  | (none) + `--dry-run`  | `juggle_cmd_doctor.py:cmd_doctor` |

What it does, in this order:

1. **Backup** — copies `~/.juggle/config.json` to `~/.juggle/config.json.bak-pre-1.16` (skip if already exists).
2. **Config migration** — reads the user's `config.json`. If `domains.initial_domain_paths` has a `vault` entry and the user has no `paths.vault` set, copies that path into `paths.vault`. Same for `domains.vault_name` → `paths.vault_name`. Then **deletes the `domains` block entirely** from the file. Writes the file atomically.
3. **DB migration check** — opens `~/.claude/juggle/juggle.db` and confirms `schema_version >= 19`. If not, invokes `JuggleDB(db_path).init_db()` to bring it forward.
4. **Report** — prints a summary: what was migrated, where the backup is, current schema_version.
5. **`--dry-run`** — prints the planned actions but writes nothing.

---

## 4. Code Paths Simplified

### `juggle_db.py — get_best_agent` before/after

**Before** (lines 980–1024):

```python
def get_best_agent(self, thread_id: str, role: str | None = None,
                   domain: str | None = None) -> dict | None:
    idle = [a for a in self.get_all_agents() if a["status"] == "idle"]
    if not idle:
        return None

    if domain:
        # Non-null domain: accept agents with matching domain or null domain
        idle = [
            a for a in idle
            if a.get("domain") is None or a.get("domain") == domain
        ]
    else:
        # Null domain thread: only fresh agents (domain=null) to avoid cross-pollination
        idle = [a for a in idle if a.get("domain") is None]

    if not idle:
        logging.info("domain filter: no idle '%s' agents, will spawn fresh", domain)
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

**After:**

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

### `juggle_cmd_agents.py — cmd_get_agent` before/after (lines ~413–430)

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

---

## 5. Config Migration

### `juggle_settings.py` — DEFAULTS change

**Remove** the entire `domains` block from `DEFAULTS` (lines ~69–77):

```python
# REMOVE THIS ENTIRE BLOCK:
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

**Extend** the existing `paths` block:

```python
"paths": {
    "data_dir": "~/.claude/juggle",
    "config_dir": "~/.juggle",
    "digest_log_dir": "~/.juggle/logs",
    "vault": "/Documents/personal",   # vault location relative to $HOME
    "vault_name": "",                  # empty = auto-derive as Path(vault).name
},
```

### `juggle_cli.py` — vault helper functions (hard cutover, no fallback)

**`_get_vault_root()` after:**

```python
def _get_vault_root() -> Path:
    from juggle_settings import get_settings
    vault_rel = get_settings()["paths"].get("vault", "/Documents/personal")
    return Path.home() / vault_rel.lstrip("/")
```

**`_get_vault_name()` after:**

```python
def _get_vault_name() -> str:
    from juggle_settings import get_settings
    explicit = get_settings()["paths"].get("vault_name", "")
    if explicit:
        return explicit
    return _get_vault_root().name
```

### `juggle_cmd_research.py` — `_get_vault_info()` after

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

The `.get("vault", "/Documents/personal")` default is a defensive backstop only — `DEFAULTS["paths"]["vault"]` always provides the value after settings merge, so this default fires only if a user has explicitly set `paths.vault` to `None` (not a normal state).

---

## 6. `/juggle:doctor` — Auto-Migration Command

### Why

Hard cutover with no in-code fallback (Mike's decision). Without a fallback, a user with a customized `~/.juggle/config.json` (vault path set under the old `domains.initial_domain_paths` schema) would silently revert to the default `/Documents/personal` on upgrade. `/juggle:doctor` is the one-shot tool that catches them.

### CLI: `juggle doctor`

`src/juggle_cmd_doctor.py` — new file. Sketch:

```python
"""Juggle CLI — config + DB migration helper for 1.16.0+ schema."""

import json
import shutil
import sqlite3
import sys
from pathlib import Path


CONFIG_PATH = Path.home() / ".juggle" / "config.json"
BACKUP_PATH = Path.home() / ".juggle" / "config.json.bak-pre-1.16"


def _migrate_config(cfg: dict) -> tuple[dict, list[str]]:
    """Return (new_cfg, changes). Pure — no I/O."""
    changes: list[str] = []
    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        return cfg, changes

    paths = cfg.setdefault("paths", {})

    # Vault path: only fill if user hasn't already set paths.vault
    if "vault" not in paths:
        initial_paths = domains.get("initial_domain_paths") or []
        for entry in initial_paths:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] == "vault":
                paths["vault"] = entry[0]
                changes.append(f"paths.vault = {entry[0]} (migrated from domains.initial_domain_paths)")
                break

    # Vault name: only fill if user hasn't already set paths.vault_name
    if "vault_name" not in paths:
        legacy_name = domains.get("vault_name", "")
        if legacy_name:
            paths["vault_name"] = legacy_name
            changes.append(f"paths.vault_name = {legacy_name} (migrated from domains.vault_name)")

    # Always remove the old domains block
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

    # 2. DB — presence-based check (juggle has no schema_version table; we look
    # for the surface left over from the pre-1.16 schema).
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

Wire into `juggle_cli.py`:

```python
# subparser
p_doctor = subparsers.add_parser("doctor", help="Migrate config + DB to current schema")
p_doctor.add_argument("--dry-run", action="store_true", help="Print actions, write nothing")
# dispatch
elif args.cmd == "doctor":
    from juggle_cmd_doctor import cmd_doctor
    sys.exit(cmd_doctor(args))
```

### Slash command: `/juggle:doctor`

`commands/doctor.md` — new file, follows the same shape as `commands/init.md`. Body:

```markdown
---
description: Migrate Juggle config + DB to the current schema (one-shot upgrade helper).
allowed-tools: Bash
---

# /juggle:doctor — Config + DB Migration

Runs the `juggle doctor` CLI which:

1. Backs up `~/.juggle/config.json` to `~/.juggle/config.json.bak-pre-1.16`.
2. Rewrites the config to move `domains.initial_domain_paths` (vault entry) → `paths.vault`, and `domains.vault_name` → `paths.vault_name`.
3. Removes the obsolete `domains` block.
4. Runs DB migrations to `schema_version >= 19` (drops `threads.domain`, `agents.domain`, `domains`, `domain_paths`).

## Run

```bash
python3 ~/github/juggle/src/juggle_cli.py doctor
```

For a preview without writes:

```bash
python3 ~/github/juggle/src/juggle_cli.py doctor --dry-run
```

Report the output. If the user wants to revert: restore `~/.juggle/config.json.bak-pre-1.16` and downgrade Juggle.
```

---

## 7. Skills/Commands Updated

### `commands/start.md`

In the CLI reference table, change the `create-thread` row and add a `doctor` row:

**Before:**

```
| `create-thread` | `<label> [--domain D]` | New topic |
```

**After:**

```
| `create-thread` | `<label>` | New topic |
| `doctor`        | `[--dry-run]` | Migrate config + DB to current schema |
```

### `commands/capture.md`

The file contains two inline Python snippets that read from `domains`. Replace both.

**VAULT_PATH snippet — before:**

```python
paths = s['domains']['initial_domain_paths']
vault_rel = next((p[0] for p in paths if p[1] == 'vault'), '/Documents/personal')
print(os.path.expanduser('~') + vault_rel)
```

**After:**

```python
vault_rel = s['paths'].get('vault', '/Documents/personal')
print(os.path.expanduser('~') + vault_rel)
```

**VAULT_NAME snippet — before:**

```python
explicit = s['domains'].get('vault_name', '')
if explicit:
    print(explicit)
else:
    paths = s['domains']['initial_domain_paths']
    vault_rel = next((p[0] for p in paths if p[1] == 'vault'), '/Documents/personal')
    print(Path(vault_rel.rstrip('/')).name)
```

**After:**

```python
explicit = s['paths'].get('vault_name', '')
if explicit:
    print(explicit)
else:
    vault_rel = s['paths'].get('vault', '/Documents/personal')
    print(Path(vault_rel.rstrip('/')).name)
```

### `commands/research.md`

**Vault-path snippet in Step 5 — before:**

```python
paths = get_settings()['domains']['initial_domain_paths']
vault = next((p[0] for p in paths if p[1] == 'vault'), None)
print(os.path.expanduser('~') + vault if vault else '')
```

**After:**

```python
vault_rel = get_settings()['paths'].get('vault', '')
print(os.path.expanduser('~') + vault_rel if vault_rel else '')
```

---

## 8. Test Plan

### Existing tests that cover changed paths

| Test file                        | Coverage                                                            | Action                                                                                       |
| -------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `tests/test_juggle_domain.py`    | All domain registry, path inference, `get_best_agent` domain filter | **Delete entire file**                                                                       |
| `tests/test_juggle_db_agents.py` | `get_best_agent` without domain arg                                 | Verify no `domain=` kwargs in call sites; add a signature test                               |
| `tests/test_data_migration.py`   | DB migration sequence                                               | **Verified clean** — file contains 0 references to `domain`; add Migrations 17–19 test only  |
| `tests/test_juggle_cli.py`       | CLI integration                                                     | Verify no `register-domain` or `--domain` invocations remain                                 |

**Verification results for Mike's open questions:**

- **Q2 — module-level `_INITIAL_DOMAINS` / `_INITIAL_DOMAIN_PATHS` references:** `grep -rn "_INITIAL_DOMAINS\|_INITIAL_DOMAIN_PATHS"` in `tests/` returns zero matches. Only `src/juggle_db.py` references them, lines 95–98 (declaration) and 315, 322 (Migration 9 body — which we are emptying). Safe to remove the module-level vars together with Migration 9's body.
- **Q4 — `tests/test_data_migration.py`:** `grep -c "domain"` returns 0 — no assertions on `threads.domain`/`agents.domain` or seed tables. No updates required to existing tests in that file; only the new Migrations 17–19 test below is needed.

### New tests to add: `tests/test_vault_path_config.py`

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

### New test for simplified `get_best_agent`

Add to `tests/test_juggle_db_agents.py`:

```python
def test_get_best_agent_no_domain_param():
    """get_best_agent no longer accepts domain keyword after cleanup."""
    import inspect
    from juggle_db import JuggleDB
    sig = inspect.signature(JuggleDB.get_best_agent)
    assert "domain" not in sig.parameters
```

### New tests: `tests/test_doctor.py`

```python
"""Tests for juggle doctor config + DB migration helper."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_cmd_doctor import _migrate_config


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

### Migration tests

Add to `tests/test_data_migration.py`:

```python
def test_migration_17_18_19_drops_domain(tmp_path):
    """Migrations 17–19 drop domain columns and tables on an old-schema DB."""
    import sqlite3
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE domains (name TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE domain_paths (path_fragment TEXT PRIMARY KEY, domain TEXT)")
    conn.commit()
    conn.close()

    from juggle_db import JuggleDB
    d = JuggleDB(db_path)
    d.init_db()

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

---

## 9. Future-Project Compatibility Note

The `threads` table after this change has columns:

```
id, session_id, topic, status, summary, key_decisions, open_questions,
last_user_intent, agent_task_id, agent_result, show_in_list,
summarized_msg_count, title, created_at, last_active,
memory_context, memory_loaded, reviewed, user_label, last_active_at
```

No column is named `project`. Adding the planned `project TEXT` column as **Migration 20** is clean:

```python
# Migration 20: add project column to threads (future)
cols = {row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()}
if "project" not in cols:
    try:
        conn.execute("ALTER TABLE threads ADD COLUMN project TEXT DEFAULT NULL")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 20 skipped: %s", e)
```

**No conflicts introduced by this spec:**

- The new `paths.vault` and `paths.vault_name` config keys are inside the settings dict, not the DB schema
- Nothing in this spec names a variable, column, or config key `project`
- The `capture.md` command already has a `--project` flag for inbox routing — that's a CLI argument, not a DB concept. When `threads.project` is added later, the CLI flag and the column will need explicit wiring. **Flag this as a naming note** when implementing Migration 20 so the author checks for ambiguity between `--project` capture routing and `threads.project` tagging.

---

## 10. Devil's Advocate

### (a) Does anyone outside the repo depend on the `domain` CLI commands?

**Finding:** Only `commands/start.md` references `--domain` — one line in the CLI reference table. No README mention, no test invocations, no skill file invocations. The SkillsMP marketplace serves `commands/start.md` directly; removing `[--domain D]` from the table is the only change needed.

**Verdict:** No external dependency. Safe to remove.

---

### (b) Will dropping `domains` / `domain_paths` tables break a prod DB that has rows?

**Writers audit:**

- `domains` table: written by `register_domain()` (called from CLI and Migration 9 seeding). CLI command confirmed never invoked (user audit). Migration 9 seeds `juggle`, `vault`, `work` on init — that's the only production data.
- `domain_paths` table: written by `add_domain_path()` (called from CLI and Migration 9). Same result — only default seed rows exist.

**Drop order:** `domain_paths` has an FK reference to `domains` (`domain TEXT NOT NULL REFERENCES domains(name)`). Drop `domain_paths` first, then `domains`. SQLite FK enforcement is off by default, but correct order prevents any surprise if it's ever enabled.

**Verdict:** Safe. The only rows are the three seed entries (`juggle`, `vault`, `work`).

---

### (c) Hard cutover risk — silent revert for custom vault paths

**Risk:** A user who set a non-default vault path in `config.json` under `initial_domain_paths` would silently revert to `/Documents/personal` after the upgrade (no in-code fallback, per Mike's hard-cutover decision).

**Likelihood:** Low. Default vault path is `/Documents/personal`; the one known deployment (Mike's) matches the default. Per Mike, user base is small enough that a breaking change in one version bump is acceptable.

**Mitigation:** `/juggle:doctor` rewrites the user's `config.json` to copy the old path into the new key, removes the obsolete `domains` block, runs DB migrations, and writes a backup. Documented in `commands/doctor.md` and in the release notes.

**Residual risk:** Users who upgrade but never run `/juggle:doctor` and have a customized vault path get silent revert. Acceptable given the user-base size; mitigation is to mention `/juggle:doctor` prominently in the release notes / changelog.

**Verdict:** Acceptable risk with the doctor in place.

---

### (d) Is `ALTER TABLE DROP COLUMN` safe across all SQLite versions Juggle targets?

**Requirement:** SQLite ≥ 3.35.0.
**Juggle minimum Python:** 3.11, which ships with SQLite 3.39.x.
**Verdict:** Safe on all supported platforms. Migration 17/18 wraps the call in `try/except OperationalError` with a warning-only skip, so even on an old SQLite the migration gracefully skips (leaving the column as dead weight rather than crashing).

---

### (e) Could the agent-pool domain filter at `juggle_db.py:981` be silently doing useful work we'd lose?

**Trace:** The filter takes the `else` branch when `domain is None`. It filters idle agents to those with `domain=None`. Since:

1. `cmd_get_agent` reads `thread.get("domain")` — always `None` (zero threads have domain set)
2. `infer_domain_from_prompt` returns `None` (the `domain_paths` table has only the three seed entries; none match real topics like "juggle-cockpit" or "lifeos")
3. `update_agent(domain=thread_domain)` stamps `domain=None` on every agent assignment — all agents have `domain=None`

The filter `[a for a in idle if a.get("domain") is None]` passes every idle agent through. Pure no-op.

**Verdict:** Zero behavioral change. Safe to remove.

---

### (f) Could `create_thread`'s `domain` parameter be called with a value anywhere we missed?

**Audit:** Only one call site: `juggle_cmd_threads.py:cmd_create_thread` — reads from `args.domain` which comes from the `--domain` CLI arg. That arg is never passed by any skill, test, or script. `db.create_thread(..., domain=domain)` with a non-None domain has never occurred in production (0 threads with domain set, confirmed by user audit).

**Verdict:** Safe to remove the parameter.

---

### (g) NEW — Module-level `_INITIAL_DOMAINS` import-time crash

**Risk:** `juggle_db.py` line 95 reads `_get_settings()["domains"]["initial_domains"]` at import time. The instant `DEFAULTS["domains"]` is removed in `juggle_settings.py`, any process that imports `juggle_db` crashes with `KeyError: 'domains'`.

**Mitigation:** In the same commit that removes `DEFAULTS["domains"]`, also remove lines 95–98 of `juggle_db.py` AND empty Migration 9's body (which is the only reader of those module vars). The plan sequences this as a single coherent change in Task 2.

**Verdict:** Manageable, but the implementer MUST NOT split the settings change and the `juggle_db.py` cleanup across two commits.

---

### (h) NEW — `/juggle:doctor` idempotency

**Risk:** If a user runs `juggle doctor` twice, the second run should be a no-op. Possible failure modes:

- Backup file already exists → overwrite would clobber a real backup
- `domains` block already removed → `_migrate_config` returns empty changes
- DB schema already at 19 → reports "already at v19"

**Mitigation:** Plan covers all three:

- Backup creation guarded by `if not BACKUP_PATH.exists()`
- `_migrate_config` returns `changes == []` when there's nothing to do
- DB check reads `schema_version` first and only re-runs if `< 19`

**Verdict:** Idempotent by construction. Test covered in `test_doctor.py:test_migrate_config_no_op_when_no_domains_block`.

---

## Open Questions

**All resolved by Mike's 2026-05-17 review:**

1. **Deprecation fallback duration** → Hard cutover, no fallback. Replace with `/juggle:doctor` auto-migration command.
2. **Removal of `_INITIAL_DOMAINS` / seed data from `juggle_db.py` module level** → Verified: no test references the module-level vars. Safe to remove together with Migration 9's body.
3. **Migration 9 — leave in place vs. delete** → Empty its body, keep the comment + version-set call. Cleaner for long-term code health than fully deleting.
4. **`test_data_migration.py` existing coverage** → Verified: 0 `domain` references. No retrofit needed; only the new Migrations 17–19 assertion test gets added.
