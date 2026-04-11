# Juggle

Multi-topic conversation orchestrator for Claude Code. Discuss one topic while background agents research or build for others — switch between threads freely without losing context.

## Install

```
/plugin marketplace add mikechen/juggle
/plugin install juggle@juggle
```

## Commands

| Command | Description |
|---------|-------------|
| `/juggle:start` | Activate juggle mode for the session |
| `/juggle:show-topics` | Show all open topics with status |
| `/juggle:resume-topic <id>` | Switch to a topic by ID |

## How It Works

1. Run `/juggle:start` — initializes the database and activates hooks
2. Talk normally — juggle detects topic shifts and automatically creates new threads
3. Send work to background — agents research/build while you keep talking
4. Get notified — completion notices appear at the next natural pause
5. Switch topics — `/juggle:resume-topic B` restores full context from the database

## Topic Status Display

```
Topics:
  [A] Auth module design        <- you are here
  [B] API rate limiting         -> agent researching... (2m ago)
  [C] Quick Q: env var config   done (results ready)
```

## Thread Status Types

DB `status` column values and what sets them:

| Status | Set by | Meaning |
|--------|--------|---------|
| `active` | `create-thread` | Normal open thread |
| `background` | `set-agent` | Background agent running |
| `done` | `complete-agent` | Work completed |
| `failed` | `fail-agent` | Agent failed |
| `archived` | `archive-thread` | Archived (hidden from list) |

## Display States

Emoji indicators shown in `show-topics` (priority: current > background > done > failed > archived > waiting > idle):

| Emoji | Condition |
|-------|-----------|
| 👉 | Current thread |
| 🏃 | Agent running (`status=background`) |
| ⏸️ | Unanswered question — last assistant message ends with `?` and no real user reply follows; applies when `status` is `active` or `done` and last assistant message has an unanswered `?` |
| ✅ | Done (`status=done`, no unanswered question) |
| ❌ | Failed (`status=failed`) |
| 🗄️ | Stale (idle >48h) — distinct from `status=archived` threads which are hidden entirely |
| 💤 | Idle >30 minutes (`last_active` older than 30m, `status=active`) |

## Limits

| Limit | Default | Env var override |
|---|---|---|
| Concurrent topics | 10 | `JUGGLE_MAX_THREADS` |
| Background agents | 20 | `JUGGLE_MAX_BACKGROUND_AGENTS` |

Set env vars in your shell profile or `~/.claude/settings.json` to override.

- 15-minute agent timeout
- Topics persist across session compactions via SQLite

## Roadmap

- [ ] `juggle watch` — terminal dashboard for `watch -n` showing current topic, agent status, and thread list
- [ ] Append `Q:` / `A:` to each response for quick at-a-glance dialogue review

## Data

Topic state is stored in `$CLAUDE_PLUGIN_DATA/juggle.db` and persists across plugin updates.
Logs are written to `$CLAUDE_PLUGIN_DATA/juggle.log`.
