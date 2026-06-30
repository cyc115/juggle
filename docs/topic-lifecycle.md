# Topic Lifecycle (v2)

Juggle threads live in one of four states:

| State    | Emoji | Meaning                                      |
| -------- | ----- | -------------------------------------------- |
| active   | 🟢    | Orchestrator focus; user engaged             |
| running  | 🏃    | Agent(s) dispatched; background work WIP     |
| closed   | ✅     | Finished; no further thread-level action     |
| archived | 🗄    | Hidden; most-recent N visible in cockpit     |

## Transitions

- `thread create`               → active
- `dispatch-agent`              → running
- `agent complete`              → closed (+ notification row)
- `action create`               → no state change; action_item created
- `thread close`                → closed (explicit)
- `agent fail` (persistent)     → closed + action_item (high priority, type=failure)
- `agent fail` (transient)      → stays running; retry loop in orchestrator
- `thread archive`              → archived
- `thread unarchive`            → active
- auto-archive (build_startup)  → archived, when closed & last_active_at < now - TTL

Default TTL: 1h (`settings.thread_auto_archive_ttl_secs = '3600'`).

## Completion commands

**agent complete** — success; writes a session-TTL notification.

```
juggle agent complete <hex6> "merged PR #412"
```

**action create** — persistent action item for the user; survives session.

```
juggle action create <hex6> "push to prod pending" --priority high --type manual_step
```

**agent fail** — failure; auto-classified transient vs persistent.

```
juggle agent fail <hex6> "AuthError: bad key"
juggle agent fail <hex6> "ETIMEDOUT" --type transient --max-retries 3
```

## User labels

Threads are referenced by an Excel-style user label (A..Z, then AA..ZZ). Labels are
assigned sequentially at `thread create`, and **never reassigned**. An archived thread
keeps its label; the slot is reserved permanently.

All CLI `<id>` arguments accept either the label (`A`, `BC`) or a 6+-character hex
prefix of the internal UUID. Agent prompts reference the hex id; context injection
and cockpit display use the label.

## Action items vs notifications

| Surface         | Persists across session? | Dismissed by        |
| --------------- | ------------------------ | ------------------- |
| notifications   | No                       | Session id mismatch |
| action_items    | Yes                      | `action ack <id>`   |
