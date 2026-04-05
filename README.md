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
2. Talk normally — juggle detects topic shifts and offers to create new threads
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

## Limits

- Max 4 concurrent topics
- Max 3 background agents
- 15-minute agent timeout
- Topics persist across session compactions via SQLite

## Data

Topic state is stored in `$CLAUDE_PLUGIN_DATA/juggle.db` and persists across plugin updates.
Logs are written to `$CLAUDE_PLUGIN_DATA/juggle.log`.
