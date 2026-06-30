---
# Juggle Architecture

## Code map (2026-06-10 refactor)

Flat `src/` with domain subpackages; every entry module bootstraps `sys.path.insert(0, src)`,
so subpackages import as top-level packages (`from dbops.threads import ...`).

| Domain | Where | What lives there |
|---|---|---|
| DB | `src/juggle_db.py` (composition root) + `src/dbops/` | `JuggleDB` assembled from mixins: `schema`, `migrations`, `session`, `threads`, `projects`, `messages`, `notifications`, `selfheal`, `agents`. All callers import `from juggle_db import ...`. |
| CLI commands | `src/juggle_cmd_*.py` | One module per command domain: threads, projects, context, research, integrate, misc; agents split into `juggle_cmd_agents_{common,worktree,pool,lifecycle,complete,tasks}` (facade: `juggle_cmd_agents.py`). Tests patch `juggle_cmd_agents_common.<sym>` — the single patch surface. |
| CLI wiring | `src/juggle_cli.py` + `src/juggle_cli_parsers_{threads,agents,misc}.py` | Entry point is parser wiring + env bootstrap only. |
| Hooks | `src/juggle_hooks.py` (dispatcher) + `src/juggle_hooks_{config,prompt,tooluse,checkpoint}.py` | Handler bodies live in the satellites; constants in `juggle_hooks_config`. |
| Watchdog | `src/juggle_watchdog.py` (hub) + `juggle_watchdog_{daemon,restart,inspect,health}.py` | `_daemon` owns the poll loop behind `scripts/juggle-agent-watchdog`; hub owns classify/recovery; `_restart` owns hot-restart staleness. |
| Cockpit | `src/juggle_cockpit.py` (app) + `juggle_cockpit_{view,model,modals,helpers,widgets,layout,profile}.py` | Textual TUI. Run `cockpit --smoke --all-viewports` after layout changes. |
| Schedules | `src/schedules/{common,autofix,dogfood,reflect}.py` | launchd/cron routines; shared plumbing in `common`. |
| Core lib | `src/juggle_{settings,context,tmux,harness,hindsight,scheduler,selfheal,smoke}.py`, `src/llm_calls.py`, `src/daemon_pidfile.py`, `src/harnesses/` | Single sources of truth: `daemon_pidfile` (singleton pids), `llm_calls` (`run_claude_p` / `llm_call`). |
| Scripts | `scripts/` | Thin argv→main() wrappers (`juggle-agent-watchdog`, `juggle-agent-monitor`) or self-contained services (`talkback`) / offline utilities (`consolidate_dbs.py`, `measure_agent_compliance.py`). |
| Tests | `tests/`, `tests/{watchdog,schedule}/` | Named by topic (`test_cli_*`, `test_db_*`, `test_tmux_*`, `test_cockpit_*`); watchdog suite grouped under `tests/watchdog/`. Regression pins are never weakened — re-target them through new seams instead. |

### Pinned entry points (do not move/rename)

- `src/juggle_cli.py` — PEP-723 `uv run --script`; referenced by 20+ `commands/*.md` and skills.
- `src/juggle_hooks.py` — invoked by path from `hooks/hooks.json` for every lifecycle hook.
- `src/juggle_watchdog.py` — mtime-watched by the watchdog daemon for hot-restart; the daemon's
  stale-source scan (`juggle_watchdog_restart._collect_mtimes`) globs **flat** `src/*.py` only —
  revisit before moving watchdog/tmux modules into subpackages.
- `scripts/juggle-agent-watchdog`, `scripts/talkback-stop-hook` — invoked by path
  (cmd_threads `_start_watchdog`, hooks.json).

### LOC gate

`scripts/loc_gate.py` (run in CI via `tests/test_loc_gate.py`) fails on any `src/**/*.py` or
python script in `scripts/` over 300 lines, except a grandfathered allowlist pinned at each
file's current size. **The allowlist may only shrink** — entries are removed/lowered as modules
are split, never raised. Adding any lines to an at-budget grandfathered file fails the gate:
split it first.

## Overview

Juggle is a multi-topic conversation orchestrator for Claude Code. It has no event loop — all state is managed via SQLite and surfaced through Claude Code lifecycle hooks.

## Lifecycle Hooks

| Hook | Trigger | Purpose |
|---|---|---|
| `UserPromptSubmit` | Every user message | Inject topic state + pending notifications as `additionalContext` |
| `PreToolUse` | Before Edit, Write, NotebookEdit, AskUserQuestion | Snapshot pre-tool state for change tracking |
| `PostToolUse` | After Read, Glob, Grep, Agent, AskUserQuestion | Track tool activity, extract [JUGGLE_THREAD:X] tags |
| `Stop` | Session end | Mark pending notifications as delivered, finalize session state |
| `SessionStart` | Resume / context compact | Restore current thread context, activate Hindsight retention |

## Data Flow

```
User message
  │
  ▼
UserPromptSubmit HOOK
  ├─ Read pending notifications from DB
  ├─ Inject "--- JUGGLE ACTIVE ---" block as additionalContext
  └─ Record prompt to messages table
  │
  ▼
Claude classifies message
  │
  ├─ Conversation/Q&A → answer inline
  ├─ Research        → dispatch background research agent
  └─ Implementation  → plan (bg) → user approval → implement (bg)
  │
  ▼
Agent(run_in_background=True, prompt="[JUGGLE_THREAD:X] ...")
  │
  ▼
PostToolUse HOOK
  ├─ Extract [JUGGLE_THREAD:X] from prompt
  ├─ Extract task_id from response
  └─ threads.status = "background", threads.agent_task_id = task_id
  │
  ▼
[Agent completes]
  │
  ▼
juggle_cli.py agent complete X "<summary>"
  ├─ threads.status = "done"
  └─ notifications row created (delivered=0)
  │
  ▼
Next UserPromptSubmit
  └─ notification surfaced in context block → LLM presents to user
  │
  ▼
Stop HOOK (session end)
  └─ notifications.delivered = 1
```

## SQLite Schema

### `threads`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Thread UUID (not limited to A/B/C/D) |
| `session_id` | TEXT | Current session ID |
| `topic` | TEXT | Human label |
| `status` | TEXT | `active` · `background` · `done` · `failed` · `closed` |
| `summary` | TEXT | LLM-written summary |
| `title` | TEXT | Short thread title |
| `key_decisions` | TEXT | JSON array of decisions |
| `open_questions` | TEXT | JSON array of pending questions |
| `last_user_intent` | TEXT | Last inferred user intent |
| `agent_task_id` | TEXT | Background agent task ID |
| `agent_result` | TEXT | Result summary from agent complete |
| `last_dispatched_task` | TEXT | UUID of last dispatched agent task |
| `last_dispatched_role` | TEXT | Role of last agent (researcher/coder/planner) |
| `last_dispatched_model` | TEXT | Model used by last agent |
| `show_in_list` | INTEGER | 0 = archived, 1 = visible |
| `summarized_msg_count` | INTEGER | Message count at last summary point |
| `user_label` | TEXT | Unique Excel-style label (A–Z, AA–AZ, etc) |
| `created_at` | TEXT | ISO timestamp |
| `last_active` | TEXT | ISO timestamp |

### `messages`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `thread_id` | TEXT FK | → threads |
| `role` | TEXT | `user` · `assistant` |
| `content` | TEXT | |
| `token_estimate` | INTEGER | `len(content) // 4` |
| `created_at` | TEXT | ISO timestamp |

### `notifications`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `thread_id` | TEXT FK | → threads |
| `message` | TEXT | Display string |
| `delivered` | INTEGER | 0 = pending · 1 = shown |
| `created_at` | TEXT | ISO timestamp |

### `session` (singleton key-value)

| Key | Values |
|---|---|
| `active` | `0` · `1` |
| `current_thread` | Thread ID or `null` |
| `session_id` | UUID |
| `started_at` | ISO timestamp |

### `agents`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | Agent UUID |
| `role` | TEXT | `researcher` · `coder` · `planner` |
| `pane_id` | TEXT | tmux pane identifier |
| `assigned_thread` | TEXT FK | → threads |
| `status` | TEXT | `idle` · `busy` |
| `context_threads` | TEXT | JSON array of thread UUIDs in context |
| `model` | TEXT | Claude model alias (e.g. sonnet, opus) |
| `last_task` | TEXT | Last sent task/prompt |
| `busy_since` | TEXT | ISO timestamp when task started |
| `watchdog_threshold_minutes` | INTEGER | Inactivity threshold, null = disabled |
| `watchdog_retried` | INTEGER | Count of watchdog-triggered retries |
| `last_activity_at` | TEXT | ISO timestamp of last activity |
| `last_send_task_pane_hash` | TEXT | Hash of last prompt for dedup |
| `last_send_task_at` | TEXT | ISO timestamp of last prompt sent |
| `created_at` | TEXT | ISO timestamp |
| `last_active` | TEXT | ISO timestamp |

### `action_items`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `thread_id` | TEXT FK | → threads |
| `message` | TEXT | Action description |
| `type` | TEXT | `question` · `manual_step` · `decision` · `failure` |
| `priority` | TEXT | `low` · `normal` · `high` |
| `created_at` | TEXT | ISO timestamp |
| `dismissed_at` | TEXT | ISO timestamp when ack'd, null if open |

### `notifications_v2`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `thread_id` | TEXT FK | → threads (nullable) |
| `message` | TEXT | Notification text |
| `session_id` | TEXT | Session UUID |
| `created_at` | TEXT | ISO timestamp |

### `agent_completions`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `role` | TEXT | Agent role |
| `duration_secs` | REAL | Task duration |
| `completed_at` | TEXT | ISO timestamp |

### `watchdog_events`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `agent_id` | TEXT FK | → agents |
| `thread_id` | TEXT FK | → threads |
| `event_type` | TEXT | Event classification |
| `snapshot_path` | TEXT | Optional snapshot file path |
| `created_at` | TEXT | ISO timestamp |

### `settings`

| Column | Type | Notes |
|---|---|---|
| `key` | TEXT PK | Config key |
| `value` | TEXT | Config value |

## Background Agent Protocol

### Dispatch (from LLM)
```python
Agent(
    prompt="[JUGGLE_THREAD:B]\nResearch the following...",
    run_in_background=True
)
```

The `[JUGGLE_THREAD:X]` tag is required — the PostToolUse hook uses it to link the agent's task_id to the thread.

### Completion (LLM calls CLI)
```bash
python3 juggle_cli.py agent complete B "3 findings from research"
# Updates: threads.agent_result, threads.status = "done"
# Creates: notification (delivered=0)
```

### Failure
```bash
python3 juggle_cli.py fail-agent B "API timeout"
# Updates: threads.status = "failed"
# Creates: notification (delivered=0)
```

## CLI Commands

Commands use a uniform **resource verb** grammar (e.g. `thread create`,
`agent complete`). Entry verbs (`start`, `stop`, `doctor`) stay flat. Legacy
flat forms (`create-thread`, `complete-agent`, …) still resolve through the
backward-compat alias shim but emit a stderr deprecation notice; see
`juggle aliases --json` for the full legacy→canonical map.

### Thread Management
| Command | Description |
|---|---|
| `start [--session-id ID]` | Init DB, activate juggle session |
| `stop` | Deactivate juggle session |
| `thread create <label>` | Create new thread |
| `thread switch <id>` | Switch to thread (archives previous) |
| `thread update <id> [--add-decision TEXT] [--add-question TEXT] [--resolve-question TEXT]` | Update thread metadata |
| `thread close <id>` | Close/archive thread |
| `thread list` | List all threads with status |
| `thread archive-candidates` | List threads eligible for archiving |
| `thread archive <id>` | Archive thread |
| `thread unarchive <id>` | Restore archived thread |
| `thread list-stale [--threshold N]` | List threads with stale summaries |
| `thread set-summarized-count <id> <count>` | Update message summary count |
| `thread messages <id> [--limit N] [--plain]` | Show messages for thread |

### Agent Management
| Command | Description |
|---|---|
| `agent spawn <role> [--model MODEL]` | Start tmux agent (researcher/coder/planner) |
| `agent list` | List all tmux agents |
| `agent get <id> [--role ROLE] [--model MODEL]` | Get idle agent or spawn new |
| `agent release <id> [--force]` | Return agent to idle pool |
| `agent decommission <id>` | Kill agent pane + remove from DB |
| `agent check` | List agents as JSON |
| `agent send-task <agent_id> <prompt_file>` | Send prompt to agent |
| `agent set-watchdog <agent_id> <minutes\|off>` | Configure watchdog threshold |
| `watchdog stop` | Kill watchdog daemon |

### Task Completion
| Command | Description |
|---|---|
| `agent complete <id> <summary> [--retain TEXT] [--open-questions JSON] [--role ROLE]` | Mark agent done, create notification |
| `agent fail <id> <error> [--type TYPE] [--max-retries N] [--recovery-dispatched]` | Mark agent failed |
| `action create <id> <message> [--type TYPE] [--priority LEVEL]` | Create action item |
| `action ack <action_id>` | Dismiss action item |
| `action list` | List open action items |
| `action next` | Switch to highest-priority action item |
| `action notify <id> <message>` | Surface notification in cockpit |

### Memory & Context
| Command | Description |
|---|---|
| `context show` | Print context string for current thread |
| `context digest [--since WHEN] [--save]` | Summarize activity since cutoff |
| `memory retain <id> <content> [--context TYPE]` | Retain content as memory |
| `vault grep <terms...>` | Search vault for keywords |
| `vault path` | Print absolute vault root path |
| `vault name` | Print vault name |

### Research & Utilities
| Command | Description |
|---|---|
| `research run <topic> [--no-web] [--verbose] [--web-results N]` | Search research KB |
| `db init` | Initialize DB schema |
| `doctor [--dry-run]` | Migrate config + DB to current schema |
| `file open <file>` | Open file in persistent nvim |

### Scheduled Routines
| Command | Description |
|---|---|
| `schedule dogfood [--dry-run]` | Run /schedule:dogfood (Sat 03:00) |
| `schedule autofix [--dry-run]` | Run /schedule:autofix (Sun 03:00) |
| `schedule reflect [--dry-run]` | Run /schedule:reflect (Mon 03:00) |

### Removed commands (no replacement)
Flagged per the grammar-migration spec §7 — these had no resource-verb home and
were dropped, not aliased:

| Removed command | Notes |
|---|---|
| `update-summary <id> <text>` | Summaries are written by the summarizer; manual save removed. Use `thread set-summarized-count` for the message-count pointer only. |
| `recall <id> <query>` | Hindsight blocking recall removed. |
| `recall-bg <id> <query>` | Hindsight async recall removed. |
| `recall-if-cold <id> <query>` | Hindsight cold-thread recall removed. |
| `mode` | No-op selector removed. |

## Status Symbols

| Symbol | Meaning |
|---|---|
| `←` | Current thread |
| `→` | Background agent running |
| `✓` | Done |
| `✗` | Failed |

## Limits

| Constraint | Value | Enforced |
|---|---|---|
| Max topics | 4 | Yes (DB raises ValueError) |
| Max background agents | 3 | No (documented only) |
| Agent timeout | 15 minutes | No (documented only) |
| Context token budget | 1500 tokens | Yes (message loading loop) |
| Context output cap | 8000 chars | Yes (string truncation) |

## File Layout

```
juggle/
├── src/
│   ├── juggle_cli.py       # CLI bridge (called by LLM via Bash)
│   ├── juggle_db.py        # SQLite state manager
│   ├── juggle_hooks.py     # Claude Code lifecycle hook handlers
│   └── juggle_context.py   # Builds additionalContext string
├── commands/
│   ├── start.md            # /juggle:start orchestration prompt
│   └── resume-topic.md     # /juggle:resume-topic prompt
├── hooks/
│   └── hooks.json          # Maps Claude Code events → Python handlers
└── .claude-plugin/
    └── plugin.json         # Plugin manifest
```
---
