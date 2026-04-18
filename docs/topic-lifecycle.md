---
# Topic Lifecycle

## Thread Statuses

| Status | Meaning |
|---|---|
| `active` | Thread is open and awaiting user interaction |
| `background` | Thread has a dispatched agent running; awaiting completion |
| `done` | Thread closed manually via `close-thread`; no further work expected |
| `failed` | Agent exited abnormally or was cleaned up as orphaned |
| `archived` | Thread hidden from cockpit and context injection; terminal state (reversible) |

---

## State Transition Diagram

```
                    create-thread
                         │
                         ▼
                      [active] ◄─────────────────────────────────┐
                         │                                        │
              dispatch agent                             agent completes
                         │                                        │
                         ▼                                        │
                    [background] ───────────────────────────────►─┘
                      │    │
         cmd_fail_agent│    │_cleanup_orphaned (no busy agent)
                       │    │
                       ▼    ▼
                    [failed]
                         │
              archive-thread (manual)
         ┌───────────────┼───────────────┐
         │               │               │
    [active]        [background]      [done]
         │               │               │
         └───────────────┴───────────────┘
                         │
                         ▼
                    [archived]
                         │
                 unarchive-thread
                         │
                         ▼
                      [active]
```

### Simplified linear view

```
active ──dispatch──► background ──complete──► active
  │                      │
close-thread          close-thread / cmd_fail_agent / _cleanup_orphaned
  │                      │
  ▼                      ▼
done                  done / failed
  │
  └── archive-thread (any non-archived status) ──► archived ──unarchive──► active
```

---

## Transitions — Triggers and Code Locations

| Transition | From | To | Trigger | Function / Command |
|---|---|---|---|---|
| Thread created | — | `active` | `/juggle:new-topic` or `create-thread` command | `cmd_create_thread()` |
| Agent dispatched | `active` | `background` | Agent tool invoked for thread | `cmd_dispatch_agent()` (or equivalent task dispatch) |
| Agent completes | `background` | `active` | `PostToolUse` hook fires after Agent tool | `cmd_complete_agent()` / PostToolUse handler |
| Manual close | `active` / `background` | `done` | `close-thread` command | `cmd_close_thread()` |
| Agent failure | `background` | `failed` | Explicit failure signal | `cmd_fail_agent()` |
| Orphan cleanup | `background` | `failed` | No busy agent found for thread on startup | `_cleanup_orphaned_threads()` in `build_startup_output()` |
| Manual archive | any non-archived | `archived` | `archive-thread` command | `cmd_archive_thread()` |
| Unarchive | `archived` | `active` | `unarchive-thread` command | `cmd_unarchive_thread()` |

---

## Cockpit + Context Injection Filter

Both the cockpit thread list and the `UserPromptSubmit` context injection use the same filter:

```sql
WHERE status != 'archived'
  AND show_in_list = 1
```

**Implications:**
- `done` and `failed` threads remain visible indefinitely until explicitly archived.
- There is no age-based expiry — a `done` thread from months ago still appears in the cockpit.
- `show_in_list = 0` suppresses a thread regardless of status (used for internal/system threads).

---

## Auto-Archive Gap

### Current behavior (manual only)

There is no automatic archival. `done` and `failed` threads accumulate in the cockpit and are injected into every context window until a user runs `archive-thread` manually. Over time this degrades context quality and clutters the UI.

### Proposed fix — `_auto_archive_stale_threads` in `build_startup_output`

The startup trigger chain is:

```
SessionStart hook
  └─► /juggle:start skill
        └─► juggle_cli.py start
              └─► cmd_start()
                    └─► build_startup_output(db)
                          ├─ _cleanup_orphaned_threads(db)   ← already exists
                          └─ _auto_archive_stale_threads(db) ← proposed addition
```

`_auto_archive_stale_threads(db)` would:

1. Call `get_archive_candidates(db)` to retrieve eligible threads.
2. Call `db.archive_thread(thread_id)` for each candidate.
3. Respect the `thread_archive_threshold_secs` config setting as the staleness threshold.

This runs automatically on every session start — no user action required.

---

## `get-archive-candidates` Criteria

A thread is a candidate for archival if **any** of the following are true:

| Criterion | Condition |
|---|---|
| Terminal status | `status = 'done'` OR `status = 'failed'` |
| Stale active | `last_active` older than `thread_archive_threshold_secs` AND `status NOT IN ('background', 'waiting')` |

**Always excluded:**

- The currently active thread.
- Threads already in `archived` status.

Threads in `background` or `waiting` are excluded from staleness archival to avoid archiving threads with in-flight agents.
