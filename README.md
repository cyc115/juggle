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

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **tmux** | 3.0+ | Background agents run in tmux panes |
| **Python** | 3.12+ | Used by CLI, hooks, cockpit |
| **uv** | any recent | For running the cockpit (`uv run ...`) |
| **Claude Code CLI** | current | Orchestrator and agent runtime |
| **OS** | macOS (primary) | Linux works; no Windows support. Talkback TTS is macOS-only |

## Quick Start

```bash
# 1. Install the plugin via Claude Code
/plugin marketplace add mikechen/juggle
/plugin install juggle@juggle

# 2. Activate juggle mode in the current session
/juggle:start

# 3. Open the live dashboard in a second terminal (optional but recommended)
uv run ~/.claude/plugins/juggle/src/juggle_cockpit.py

# 4. Talk normally. Juggle will detect topic shifts and dispatch agents.
#    Example: mention two unrelated things in one message and watch a
#    second thread + background agent spin up.

# 5. List topics any time
/juggle:show-topics
```

## Architecture

```
                ┌────────────────────────────────────────────────────┐
                │           Claude Code (main session)               │
                │           = ORCHESTRATOR thread                    │
                └──┬─────────────────────────────────────────────┬───┘
                   │ UserPromptSubmit / PostToolUse hooks        │
                   │ inject context, auto-approve, route tasks   │
                   ▼                                             ▼
         ┌─────────────────────┐                      ┌──────────────────┐
         │   juggle_cli.py     │◄─────state ops──────►│   SQLite DB      │
         │  (create-thread,    │                      │  threads,        │
         │   spawn-agent,      │                      │  messages,       │
         │   send-task, …)     │                      │  notifications,  │
         └──────────┬──────────┘                      │  agents,         │
                    │ tmux send-keys / split-window   │  action_items    │
                    ▼                                 └──────────────────┘
         ┌─────────────────────────────────────────┐             ▲
         │  tmux session "juggle" (220x50)         │             │
         │  ┌───────────┐  ┌───────────┐  ┌──────┐ │             │
         │  │ agent %5  │  │ agent %6  │  │ ...  │ │──writes────┘
         │  │ researcher│  │  coder    │  │      │ │   results
         │  │ (claude)  │  │ (claude)  │  │      │ │
         │  └───────────┘  └───────────┘  └──────┘ │
         └─────────────────────────────────────────┘
                                  ▲
                                  │ read-only snapshot every 1s
                                  │
                   ┌─────────────────────────────┐
                   │  Cockpit (juggle_cockpit.py)│
                   │  Topics │ Actions │ Agents  │
                   └─────────────────────────────┘
```

No event loop. State lives in SQLite; Claude Code lifecycle hooks (`UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionStart`) do all the routing. See [`docs/architecture.md`](docs/architecture.md) for the full data flow and schema.

## Install

```
/plugin marketplace add mikechen/juggle
/plugin install juggle@juggle
```

## Plugin System

Juggle ships as a Claude Code plugin (see `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`). Plugins bundle slash commands, hooks, skills, and supporting scripts behind a single install step.

**Install any plugin:**

```
/plugin marketplace add <owner>/<repo>
/plugin install <name>@<marketplace>
```

**Where it lives:** Installed plugins are extracted under `~/.claude/plugins/<name>/`. Juggle's DB and logs live at `$CLAUDE_PLUGIN_DATA/juggle.db` / `juggle.log` so they survive plugin upgrades.

**Built-in to this repo:**

- `juggle` (this plugin) — the multi-topic orchestrator itself. Provides `/juggle:start`, `/juggle:show-topics`, `/juggle:resume-topic`, `/juggle:show-agents`, `/juggle:archive-topics`, `/juggle:memory-start`, `/juggle:memory-stop`, `/juggle:remember`, `/juggle:toggle-talkback`, `/juggle:init`.

## Commands

| Command | Description |
|---------|-------------|
| `/juggle:start` | Activate juggle mode for the session |
| `/juggle:show-topics` | Show all open topics with status |
| `/juggle:resume-topic <id>` | Switch to a topic by ID |
| `/juggle:show-agents` | List all background agents and their state |
| `/juggle:archive-topics` | Archive completed or stale topics |
| `/juggle:remember <text>` | Explicitly retain a memory to Hindsight |
| `/juggle:toggle-talkback` | Toggle TTS voice notifications |

Under the hood every command shells out to `python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py <subcommand>`. Run `juggle_cli.py --help` for the full subcommand list (create-thread, switch-thread, spawn-agent, send-task, complete-agent, digest, recall, grep-vault, and more).

## How It Works

1. Run `/juggle:start` — initializes the database and activates hooks
2. Talk normally — juggle detects topic shifts and automatically creates new threads
3. Send work to background — agents research/build while you keep talking
4. Get notified — completion notices appear at the next natural pause
5. Switch topics — `/juggle:resume-topic B` restores full context from the database

## Cockpit

A live terminal dashboard for the current juggle session. Read-only — it never writes to the DB.

**Launch:**

```bash
uv run src/juggle_cockpit.py           # from the plugin source dir
# or, once installed as a plugin:
uv run ~/.claude/plugins/juggle/src/juggle_cockpit.py
```

Refreshes once per second (configurable via `cockpit.refresh_interval_secs`). Exit with Ctrl-C.

**Three columns (plus notifications strip):**

- **Topics** — every non-archived thread, with status glyph, label (`A`–`ZZ`), and title.
- **Action Items** — persistent follow-ups created by `request-action` or by failed agents. Survive sessions until you `ack-action` them.
- **Agents** — every tmux agent in the pool, with role, model, assigned thread, and idle age.
- **Notifications** — transient completion messages and warnings (session-scoped).

**Layout mockup:**

```
┌─ Topics ─────────────────┬─ Action Items ──────────────────┬─ Agents ───────────┐
│ 👉 [A] Auth module       │ ❗ [B] fix rate-limit env var   │ 🏃 [A] coder  s 02m│
│ 🏃 [B] API rate limiting │ 📌 [C] follow up with vendor     │ 💤 [—] researcher  │
│ ✅ [C] Env var config    │                                  │    (idle 14m)      │
│ 💤 [D] Refactor models   │                                  │                    │
│ 🗄️ [E] Old spike         │                                  │                    │
├──────────────────────────┴──────────────────────────────────┴────────────────────┤
│  Notifications: ✅ Agent on [C] finished — "env var is API_RATE_LIMIT_RPS"       │
└──────────────────────────────────────────────────────────────────────────────────┘
```

**Status legend:**

| Glyph | Meaning |
|---|---|
| 👉 | Current (focused) thread |
| 🏃 | Running — background agent dispatched |
| ⏸️ | Unanswered question from the assistant |
| ✅ | Closed / done |
| ❌ | Failed |
| 💤 | Idle > 30 min (still active) |
| 🗄️ | Stale — idle > 48 h (candidate for archive) |
| ❗ | Action item, high priority |
| 📌 | Action item, normal priority |

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

## Configuration Reference

Defaults live in `src/juggle_settings.py`. Override by editing `~/.juggle/config.json` (deep-merged) or via env vars (highest precedence).

| Key | Default | Meaning |
|---|---|---|
| `max_threads` | `10` | Concurrent open topics. Env: `JUGGLE_MAX_THREADS` |
| `max_agents` | `20` | Concurrent background agents. Env: `JUGGLE_MAX_BACKGROUND_AGENTS` |
| `agent_idle_ttl_secs` | `43200` (12 h) | Idle agents reaped after this. |
| `message_history_token_budget` | `1500` | Tokens of history injected per prompt. |
| `tmux.session_name` | `"juggle"` | tmux session that hosts agent panes. |
| `tmux.session_width` / `session_height` | `220` / `50` | Session geometry. |
| `tmux.agent_idle_detection_secs` | `30` | Idle threshold. Env: `JUGGLE_IDLE_THRESHOLD_SECS` |
| `cockpit.refresh_interval_secs` | `1.0` | Cockpit redraw rate. |
| `cockpit.thread_idle_threshold_secs` | `1800` (30 m) | 💤 glyph threshold. |
| `cockpit.thread_archive_threshold_secs` | `172800` (48 h) | 🗄️ stale glyph threshold. |
| `agent.claude_launch_command` | `"claude --dangerously-skip-permissions"` | How each agent pane starts Claude Code. |
| `hindsight.enabled` | `false` | Enable long-term memory integration. |
| `talkback.enabled` | `false` | Enable TTS completion notifications. |
| `paths.data_dir` | `~/.claude/juggle` | DB and log directory. |

Full settings object (including nested sections for Hindsight, talkback, and domain seeds) is documented inline in `src/juggle_settings.py`.

## Troubleshooting

**`tmux not found` / session not found.** Install tmux (`brew install tmux`). Agent spawn calls `ensure_session()` which (re)creates the `juggle` session on demand; if you killed it manually, the next `get-agent` will recreate it.

**Agent dispatched but nothing happened / messages pasted in the wrong terminal.** The CLI now refuses to `send_task` with an empty `pane_id` (prevents pasting into your own shell by mistake). If you see `send_task called with empty pane_id`, the DB row for that agent is corrupt — run `/juggle:show-agents` and `decommission-agent <id>` to clean it up, then let the pool spawn a fresh one.

**Background agent stuck on a permission prompt.** Juggle's `UserPromptSubmit` hook inspects panes and auto-sends `2 + Enter` for safe prompts. If an agent still hangs, the prompt is outside the allowlist — check the pane directly (`tmux attach -t juggle`) and approve manually, or add `--dangerously-skip-permissions` to `settings["agent"]["claude_launch_command"]` (the default already includes it).

**Talkback: "audio device error" or silence.** Stop and restart talkback (`/juggle:toggle-talkback` twice). The server falls back through a device chain; if your output device changed (Bluetooth disconnect, USB unplug) the server needs to re-bind.

**Orchestrator keeps reading files instead of dispatching.** The orchestrator guardrail hook warns you when it detects direct tool use in the main thread. Re-read `CLAUDE.md` / `AGENTS.md` in the repo — the orchestrator's job is to route, not to do the work.

**DB or logs missing after upgrade.** Both live under `$CLAUDE_PLUGIN_DATA` (typically `~/.claude/plugins/juggle/data/`). They're preserved across `/plugin` reinstalls. If truly gone, `/juggle:start` runs migrations and recreates the schema.

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
