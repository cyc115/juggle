---
# Juggle Architecture

## Overview

Juggle is a multi-topic conversation orchestrator for Claude Code. It has no event loop — all state is managed via SQLite and surfaced through Claude Code lifecycle hooks.

## Lifecycle Hooks

| Hook | Trigger | Purpose |
|---|---|---|
| `UserPromptSubmit` | Every user message | Inject topic state + pending notifications as `additionalContext` |
| `PostToolUse` | After Agent tool completes | Link `[JUGGLE_THREAD:X]` tag → task_id in DB |
| `Stop` | Session end | Mark pending notifications as delivered |
| `SessionStart` | Resume / context compact | Restore current thread context |

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

### `threads` (max 4 rows: A/B/C/D)

| Column | Type | Notes |
|---|---|---|
| `thread_id` | TEXT PK | A, B, C, or D |
| `session_id` | TEXT | Current session |
| `topic` | TEXT | Human label |
| `status` | TEXT | `active` · `background` · `done` · `failed` · `closed` |
| `summary` | TEXT | LLM-written summary on switch |
| `key_decisions` | TEXT | JSON array |
| `open_questions` | TEXT | JSON array |
| `agent_task_id` | TEXT | Background agent task ID |
| `agent_result` | TEXT | Result summary from complete-agent |
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

### `shared_context`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `context_type` | TEXT | `decision` · `fact` · `note` |
| `content` | TEXT | |
| `source_thread` | TEXT | FK → threads |
| `created_at` | TEXT | ISO timestamp |

### `session` (singleton key-value)

| Key | Values |
|---|---|
| `active` | `0` · `1` |
| `current_thread` | `A` · `B` · `C` · `D` · `null` |
| `started_at` | ISO timestamp |

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

| Command | Description |
|---|---|
| `start` | Init DB, set active, auto-create Topic A |
| `create-thread <label>` | Create new thread (max 4) |
| `switch-thread <id>` | Switch current thread |
| `update-summary <id> <text>` | Save thread summary before switch |
| `complete-agent <id> <summary>` | Mark agent done, queue notification |
| `fail-agent <id> <error>` | Mark agent failed, queue notification |
| `show-topics` | Print thread table with status symbols |

## Status Symbols (show-topics)

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
│   ├── show-topics.md      # /juggle:show-topics prompt
│   └── resume-topic.md     # /juggle:resume-topic prompt
├── hooks/
│   └── hooks.json          # Maps Claude Code events → Python handlers
└── .claude-plugin/
    └── plugin.json         # Plugin manifest
```
---
