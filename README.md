# Juggle

Claude Code can only hold one conversation at a time. You're designing an auth module, you need to look up rate-limiting best practices, and you want to check an env var config, but each detour blows away the context you've been building. You either serialize everything or lose your train of thought.

Juggle fixes this. It gives Claude Code parallel conversation threads backed by persistent memory, so you can work on multiple topics simultaneously without losing context on any of them.

### How it works in practice

You're building a feature and hit a question about API rate limits. Instead of derailing your current thread:

1. You mention rate limiting. Juggle detects the topic shift and opens a new thread.
2. A background agent spins up in a tmux pane to research rate-limiting patterns while you keep designing.
3. When the agent finishes, you get a notification at the next natural pause. Your original thread is untouched.
4. You switch back and forth between threads freely. Context is restored from SQLite, not from your memory.

No copy-pasting prompts. No "where was I?" after a tangent. Just type naturally and let Juggle manage the concurrency.

### Key capabilities

**Background agents via tmux.** Dispatch research, code generation, or analysis to background agents running in tmux panes. They work in parallel while your main conversation continues uninterrupted. Up to 20 concurrent agents.

**Persistent multi-topic threads.** Every thread is stored in SQLite with full message history. Session compactions, restarts, and context window limits don't erase your work. Pick up any topic exactly where you left off.

**Auto-approver.** Background agents get stuck on permission prompts ("Do you want to allow..."). Juggle's `UserPromptSubmit` hook detects blocked panes and automatically sends approval keystrokes, so agents don't stall while you're focused elsewhere.

**Orchestrator guardrails.** The main thread acts as a coordinator, not a worker. Juggle warns you if the orchestrator uses file tools directly instead of delegating to agents, keeping the separation clean.

### What you get back

Time and focus. Instead of one serial conversation that context-switches between topics, you get a workspace where multiple lines of work progress in parallel. The context for each thread is machine-managed, not human-managed. You stop being the bottleneck.

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

## State Management

Each thread moves through four states. See [docs/topic-lifecycle.md](docs/topic-lifecycle.md) for full details.

### Thread States

| State | Meaning |
|-------|---------|
| `active` | Orchestrator focus; user engaged |
| `running` | Background agent(s) dispatched; work in progress |
| `closed` | Work finished; visible for 24 h then auto-archived |
| `archived` | Hidden from active work; accessible by label to reopen |

### State Diagram

```
create-thread          dispatch-agent
      │                      │
      ▼                      ▼
  ┌────────┐  dispatch  ┌─────────┐
  │ active │ ─────────▶ │ running │ ◀── hook retry (transient fail)
  │  🟢    │ ◀───────── │   🏃    │
  └────────┘  focus      └─────────┘
      │                      │
      │ close-thread          │ complete-agent
      │ (explicit)            │
      ▼                      ▼
  ┌────────────────────────────┐
  │          closed ✅          │
  └────────────────────────────┘
                │
                │ auto-archive (last_active_at + 24 h TTL)
                ▼
  ┌────────────────────────────┐
  │         archived 🗄        │
  └────────────────────────────┘
```

`archive-thread` skips directly to `archived` from any state. `unarchive-thread` restores to `active`.

### Thread IDs

Threads have two identifiers:

- **User label** (`A`–`ZZ`, Excel-style base-26) — shown in the cockpit, used in CLI commands. Never reassigned; archived threads keep their label permanently.
- **Internal ID** (6-char hex, e.g. `a3f2bc`) — DB primary key, used in agent prompts and logs.

CLI commands accept either form: `switch-thread A` and `switch-thread a3f2bc` resolve to the same thread.

### Completion Routing

Agents signal outcomes via three explicit commands (not string heuristics):

| Command | Effect |
|---------|--------|
| `complete-agent <id> "<result>"` | Thread → `closed`; creates a session-scoped notification (informational, auto-cleared next session) |
| `request-action <id> "<what to do>"` | Creates a persistent `action_item` (survives sessions until dismissed with `ack-action`) |
| `fail-agent <id> "<error>"` | Hook classifies failure: transient → auto-retry (thread stays `running`); persistent → `action_item` with `priority=high` |

### Auto-Archive

- **Explicit close** (`close-thread`): thread enters `closed` immediately; auto-archives after 24 h idle.
- **Agent-completed close** (`complete-agent`): same TTL applies from `last_active_at`.
- TTL default: 24 h. Override via `settings["thread_auto_archive_ttl_secs"]`.

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
