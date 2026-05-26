---
# Juggle Architecture

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
juggle_cli.py complete-agent X "<summary>"
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
| `agent_result` | TEXT | Result summary from complete-agent |
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
python3 juggle_cli.py complete-agent B "3 findings from research"
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

### Thread Management
| Command | Description |
|---|---|
| `start [--session-id ID]` | Init DB, activate juggle mode |
| `stop` | Deactivate juggle mode |
| `create-thread <label>` | Create new thread |
| `switch-thread <id>` | Switch to thread (archives previous) |
| `update-summary <id> <text>` | Save thread summary |
| `update-meta <id> [--add-decision TEXT] [--add-question TEXT] [--resolve-question TEXT]` | Update thread metadata |
| `close-thread <id>` | Close/archive thread |
| `show-topics` | List all threads with status |
| `get-archive-candidates` | List threads eligible for archiving |
| `archive-thread <id>` | Archive thread |
| `unarchive-thread <id>` | Restore archived thread |
| `get-stale-threads [--threshold N]` | List threads with stale summaries |

### Agent Management
| Command | Description |
|---|---|
| `spawn-agent <role> [--model MODEL]` | Start tmux agent (researcher/coder/planner) |
| `list-agents` | List all tmux agents |
| `get-agent <id> [--role ROLE] [--model MODEL]` | Get idle agent or spawn new |
| `release-agent <id> [--force]` | Return agent to idle pool |
| `decommission-agent <id>` | Kill agent pane + remove from DB |
| `set-agent <id> <task_id>` | Link agent task to thread |
| `check-agents` | List agents as JSON |
| `send-task <agent_id> <prompt_file>` | Send prompt to agent |
| `set-watchdog <agent_id> <minutes\|off>` | Configure watchdog threshold |
| `stop-watchdog` | Kill watchdog daemon |

### Task Completion
| Command | Description |
|---|---|
| `complete-agent <id> <summary> [--retain TEXT] [--open-questions JSON] [--role ROLE]` | Mark agent done, create notification |
| `fail-agent <id> <error> [--type TYPE] [--max-retries N] [--recovery-dispatched]` | Mark agent failed |
| `request-action <id> <message> [--type TYPE] [--priority LEVEL]` | Create action item |
| `ack-action <action_id>` | Dismiss action item |
| `list-actions` | List open action items |
| `notify <id> <message>` | Surface notification in cockpit |

### Memory & Context
| Command | Description |
|---|---|
| `get-context` | Print context string for current thread |
| `recall <id> <query>` | Recall memories from Hindsight (blocking) |
| `recall-bg <id> <query>` | Recall memories async, return immediately |
| `recall-if-cold <id> <query>` | Recall only if thread is cold |
| `retain <id> <content> [--context TYPE]` | Retain content as memory |
| `grep-vault <terms...> [--vault-path PATH]` | Search vault for keywords |
| `digest [--since WHEN] [--save]` | Summarize activity since cutoff |

### Research & Utilities
| Command | Description |
|---|---|
| `research <topic> [--no-web] [--verbose] [--web-results N]` | Search research KB |
| `get-messages <id> [--limit N] [--plain]` | Show messages for thread |
| `init-db` | Initialize DB schema |
| `doctor [--dry-run]` | Migrate config + DB to current schema |
| `next-action` | Switch to highest-priority action item |
| `set-summarized-count <id> <count>` | Update message summary count |
| `open-in-editor <file>` | Open file in persistent nvim |

### Scheduled Routines
| Command | Description |
|---|---|
| `schedule-dogfood [--dry-run]` | Run /schedule:dogfood (Sat 03:00) |
| `schedule-autofix [--dry-run]` | Run /schedule:autofix (Sun 03:00) |
| `schedule-reflect [--dry-run]` | Run /schedule:reflect (Mon 03:00) |

### Internal
| Command | Description |
|---|---|
| `record-pending-decision --tool-use-id ID --questions-json JSON` | Record user decision questions |
| `clear-pending-decision --tool-use-id ID` | Clear pending decisions |

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
