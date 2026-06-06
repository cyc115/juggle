<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/juggle-logo-dark.svg">
  <img src="docs/assets/juggle-logo.svg" alt="Juggle" width="380">
</picture>

**Parallel conversation threads for Claude Code.**

[![Version](https://img.shields.io/badge/version-1.28.2-2563eb.svg)](.claude-plugin/plugin.json)
[![Python](https://img.shields.io/badge/python-3.12+-f59e0b.svg)](https://www.python.org/)
[![tmux](https://img.shields.io/badge/tmux-3.0+-22c55e.svg)](https://github.com/tmux/tmux)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-94a3b8.svg)](#prerequisites)

</div>

![Juggle Cockpit — live orchestration dashboard](docs/images/juggle-in-action.png)

> **What you're seeing:** orchestrator (top-left) dispatching parallel coders by writing task files, nvim (top-right) holding open context, and the **Cockpit v2** (full-width bottom) tracking Topics, Action Items, and live Agents. Threads `[LJ]` and `[LK]` here are critiquing the same TF provider examples in parallel — one via Claude (juggle), one via Codex.

---

Claude Code runs one conversation at a time. A research detour, a parallel build, an unrelated question — each one competes for the same context window, and switching means losing your place.

Juggle turns a single Claude Code session into a multi-track workspace. Each topic lives in its own persistent thread (SQLite-backed, survives restarts and compactions), background agents do the heavy lifting in tmux panes while you stay focused, and the Cockpit dashboard shows every topic, action item, and live agent at a glance. When an agent finishes, a notification surfaces at the next natural pause — your main thread was never interrupted.

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| **tmux** | 3.0+ | Background agents run in tmux panes |
| **Python** | 3.12+ | CLI, hooks, cockpit |
| **uv** | any | Required to launch the cockpit |
| **Claude Code CLI** | current | Orchestrator and agent runtime |
| **OS** | macOS / Linux | TTS (talkback) is macOS-only |

## Quickstart

```bash
# 1. Install
/plugin marketplace add mikechen/juggle
/plugin install juggle@juggle

# 2. Activate in any Claude Code session
/juggle:start

# 3. Open the live dashboard in a second terminal
uv run ~/.claude/plugins/juggle/src/juggle_cli.py cockpit

# 4. Dispatch your first background agent
/juggle:delegate
```

After `/juggle:start`, talk normally. Juggle detects topic shifts and opens new threads automatically. Use `/juggle:delegate` to send explicit work to a background agent.

## How it works

- **Topics** — each line of work gets a label (`A`–`ZZ`), its own SQLite-backed message history, and an independent context window. Switch anytime with `/juggle:resume-topic`.
- **Agents** — background workers (researcher / planner / coder) run in tmux panes, up to 20 concurrent. An auto-approver handles permission prompts so agents don't stall while you're focused elsewhere.
- **Cockpit** — a live dashboard (Topics | Action Items | Agents) updated every second. Textual-based with mouse drag-to-resize between panels (tmux mouse mode required).
- **Action Items** — persistent follow-ups created by agents or manually. Survive sessions until dismissed from the cockpit.
- **Hindsight memory** — opt-in long-term memory across sessions. Enable via `hindsight.enabled` in `~/.juggle/config.json`. See [docs/architecture.md](docs/architecture.md).

## Slash commands

| Command | Description |
|---------|-------------|
| `/juggle:start` | Activate juggle mode for the session |
| `/juggle:delegate` | Wizard: pick role, write prompt, dispatch agent |
| `/juggle:resume-topic <id>` | Switch to a topic, restoring full context |
| `/juggle:remember <text>` | Explicitly save something to Hindsight memory |
| `/juggle:toggle-talkback` | Toggle TTS voice notifications (macOS) |

Full catalog: [`commands/`](commands/)

## Cockpit

```bash
uv run ~/.claude/plugins/juggle/src/juggle_cockpit.py
# or
uv run ~/.claude/plugins/juggle/src/juggle_cli.py cockpit
```

Three columns: **Topics** (status + label + title), **Action Items** (persistent follow-ups), **Agents** (role, model, assigned thread, idle age). Refreshes every second. Read-only — never writes to the DB.

## Configuration

Defaults in `src/juggle_settings.py`. Override in `~/.juggle/config.json` (deep-merged) or via env vars:

| Key | Default | Effect |
|-----|---------|--------|
| `max_threads` | `10` | Concurrent open topics (`JUGGLE_MAX_THREADS`) |
| `max_agents` | `20` | Concurrent background agents (`JUGGLE_MAX_BACKGROUND_AGENTS`) |
| `message_history_token_budget` | `1500` | Thread history tokens injected per agent prompt |
| `hindsight.enabled` | `false` | Long-term memory across sessions |
| `talkback.enabled` | `false` | TTS completion notifications (macOS) |

Data and logs: `$CLAUDE_PLUGIN_DATA/juggle.db` and `juggle.log` — preserved across plugin upgrades.

## Docs

- [Architecture](docs/architecture.md) — data flow, SQLite schema, hook lifecycle
- [Topic lifecycle](docs/topic-lifecycle.md) — states, transitions, auto-archive rules
- [Agent context injection](docs/agent-context-injection.md) — how context reaches dispatched agents
- [Commands](commands/) — full slash command catalog
- [Changelog](CHANGELOG.md)
