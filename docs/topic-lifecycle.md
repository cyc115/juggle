# Topic Lifecycle (v2)

Juggle threads live in one of four states:

| State    | Emoji | Meaning                                      |
| -------- | ----- | -------------------------------------------- |
| active   | 🟢    | Orchestrator focus; user engaged             |
| running  | 🏃    | Agent(s) dispatched; background work WIP     |
| closed   | ✅     | Finished; no further thread-level action     |
| archived | 🗄    | Hidden; most-recent N visible in cockpit     |

## Transitions

- `create-thread`               → active
- `dispatch-agent`              → running
- `complete-agent`              → closed (+ notification row)
- `request-action`              → no state change; action_item created
- `close-thread`                → closed (explicit)
- `fail-agent` (persistent)     → closed + action_item (high priority, type=failure)
- `fail-agent` (transient)      → stays running; retry loop in orchestrator
- `archive-thread`              → archived
- `unarchive-thread`            → active
- auto-archive (build_startup)  → archived, when closed & last_active_at < now - TTL

Default TTL: 24h (`settings.thread_auto_archive_ttl_secs = '86400'`).

## Completion commands

**complete-agent** — success; writes a session-TTL notification.

```
juggle complete-agent <hex6> "merged PR #412"
```

**request-action** — persistent action item for the user; survives session.

```
juggle request-action <hex6> "push to prod pending" --priority high --type manual_step
```

**fail-agent** — failure; auto-classified transient vs persistent.

```
juggle fail-agent <hex6> "AuthError: bad key"
juggle fail-agent <hex6> "ETIMEDOUT" --type transient --max-retries 3
```

## User labels

Threads are referenced by an Excel-style user label (A..Z, then AA..ZZ). Labels are
assigned sequentially at `create-thread`, and **never reassigned**. An archived thread
keeps its label; the slot is reserved permanently.

All CLI `<id>` arguments accept either the label (`A`, `BC`) or a 6+-character hex
prefix of the internal UUID. Agent prompts reference the hex id; context injection
and cockpit display use the label.

## Action items vs notifications

| Surface         | Persists across session? | Dismissed by        |
| --------------- | ------------------------ | ------------------- |
| notifications   | No                       | Session id mismatch |
| action_items    | Yes                      | `ack-action <id>`   |
