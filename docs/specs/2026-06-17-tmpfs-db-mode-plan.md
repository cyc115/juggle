# Juggle tmpfs In-Memory DB Mode

**Date:** 2026-06-17  
**Status:** Implementing

## Why

`juggle.db` corrupts under concurrent multi-process writes on copy-on-write
filesystems (btrfs / zfs / overlay).  Root cause: COW rewrites page-blocks
mid-write, defeating SQLite's WAL guarantees.

Fix: move the LIVE db to tmpfs (RAM, correct POSIX locking, non-COW) so normal
multi-process SQLite + WAL work correctly.  A standalone supervised daemon
flushes RAM → durable disk every ~10 s.

OPT-IN via `db.mode = "tmpfs"`.  Default is `"direct"` = exactly current
behaviour, zero change for existing users.

## macOS Decision

macOS has no `/dev/shm`.  On macOS, `tmpfs` mode **falls back** to `direct`
mode with a clear logged warning.  No hdiutil RAM-disk.  tmpfs mode is effective
on Linux (`/dev/shm`).

## Settings Added

```json
{
  "db": {
    "mode": "direct",          // "direct" | "tmpfs"
    "tmpfs_dir": "/dev/shm",
    "flush_interval_s": 10
  }
}
```

## Architecture

```
live_path  = ${tmpfs_dir}/juggle-${instance}.db   (tmpfs, all writes)
durable_path = ~/.claude/juggle/juggle.db          (disk, flushed every N s)
```

### Components

| File | Responsibility |
|------|---------------|
| `src/juggle_db_path.py` | Pure resolver: (mode, tmpfs_dir) → {live_path, durable_path} |
| `src/juggle_db_bootstrap.py` | On first connect in tmpfs mode: copy durable→tmpfs, run migrations |
| `src/juggle_cmd_db_flush.py` | `juggle db flush`: --once, --status, loop daemon, --install-supervisor |
| `src/juggle_settings.py` | Add `db` section to DEFAULTS |
| `src/juggle_db.py` | Wire bootstrap into `__init__` |
| `src/juggle_cmd_doctor.py` | Fix hardcoded DB_PATH → resolver |

## Tasks (TDD order)

0. **Spec doc** — this file.
1. **Settings**: add `db.mode/tmpfs_dir/flush_interval_s` defaults + tests.
2. **`juggle_db_path.py`**: pure resolver, macOS fallback, tests.
3. **Fix DB_PATH usages**: doctor + other scattered connects use resolver.
4. **`juggle_db_bootstrap.py`**: copy durable→live, run migrations, integrity_check.
5. **Wire bootstrap** into `JuggleDB.__init__`.
6. **`juggle_cmd_db_flush.py`**: `--once` flush, `--status`, daemon loop, `--install-supervisor`.
7. **Split-brain guard**: hard-fail at startup if tmpfs_dir missing/unwritable.
8. **`juggle:init` integration**: configure db.mode, install flusher, bootstrap.
9. **Cockpit indicator**: "last flush: Ns ago" from `db flush --status`; alert if stale.
10. **Version bump** to 1.71.0.

## Contracts

### `resolve_db_paths(mode, tmpfs_dir, durable_path, instance_id)` → `DbPaths`
```python
@dataclass
class DbPaths:
    live: Path       # where JuggleDB connects
    durable: Path    # where flusher writes
    mode: str        # "direct" or "tmpfs" (after fallback)
```

### `bootstrap_tmpfs(live_path, durable_path)` → None
- If live absent: copy durable→live (sqlite backup API), run migrations
- If live present and fresh: noop
- Always: PRAGMA integrity_check on live

### Flush protocol
- `sqlite3.Connection.backup(dest)` live → `durable.tmp`
- `os.replace(durable.tmp, durable)` — atomic
- Interrupted flush leaves durable intact (only replaces on success)

## Safety

- DEFAULT mode=direct: zero change for existing users
- All tests use isolated tmp_path DBs; never touch `~/.claude/juggle/juggle.db`
- macOS: fallback to direct + warn (no hdiutil)
- Split-brain guard: hard-fail if mode=tmpfs and tmpfs_dir unwritable
