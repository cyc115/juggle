# Auto-Summary Generation for Juggle Threads

**Date:** 2026-04-06
**Status:** Draft
**Scope:** ~80 LOC across 5 files + prompt edits

---

## Problem

Thread summaries in `/juggle:show-topics` show "no summary yet" for most threads because
summaries are only written in two places today:

1. `update-summary` CLI — manual, requires user or orchestrator to remember
2. `complete-agent` — only fires when a background agent finishes; misses direct conversation

The result: topic switching is disorienting because there's no recap of where you left off.

---

## Goals

- Summaries stay current without user or orchestrator effort
- Both sides of conversation (user + assistant) captured reliably
- No latency added to the main conversation
- Low token cost (~$0.001/summarization using Haiku)
- Works through context compaction and session restarts

---

## Architecture

### Overview

```
UserPromptSubmit ──► [stale flag computed] ──► JUGGLE ACTIVE block
                                                     │
                                                     ▼
                                          Orchestrator sees [SUMMARY STALE]
                                                     │
                                          Spawns Haiku background agent
                                                     │
                                          Haiku reads messages table
                                          (both sides, via Stop hook capture)
                                                     │
                                          Writes updated summary to DB
                                                     │
Stop hook ──────────────────────────────► writes last_assistant_message to messages
```

### Key Invariant

The `messages` table is the single source of truth for conversation history.
User turns are captured by `UserPromptSubmit`. Assistant turns are captured by the `Stop` hook
using the `last_assistant_message` field now available in the Stop hook payload.

---

## Components

### 1. Data Layer — `juggle_db.py`

**New column on `threads`:**
```sql
summarized_msg_count  INTEGER DEFAULT 0
```

Tracks how many total messages existed when the summary was last written.
Delta = `current_message_count - summarized_msg_count`.
When delta ≥ 3 (counting only substantive user messages), summary is stale.

**Migration:** `ALTER TABLE threads ADD COLUMN summarized_msg_count INTEGER DEFAULT 0`
Applied in `init_db()` via a safe `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern.

**New methods:**
- `get_message_count(thread_id, exclude_junk=True) → int` — counts non-junk user messages
- `set_summarized_count(thread_id, count)` — called by Haiku agent after writing summary
- `get_stale_threads(threshold=3) → list[dict]` — returns threads where delta ≥ threshold

**Junk message definition** (excluded from count):
- Role is not `user`
- Content starts with `<task-notification`
- Content contains `task-id`
- Content starts with `/` (slash commands)

---

### 2. Assistant Message Capture — `juggle_hooks.py`

**Resolves the existing TODO in `handle_stop`.**

The Stop hook payload now includes `last_assistant_message`. On every Stop event:

```python
def handle_stop(data: dict) -> None:
    last_msg = data.get("last_assistant_message", "").strip()
    if last_msg and is_active():
        db = get_db()
        thread_id = db.get_current_thread()
        if thread_id is not None:
            db.add_message(thread_id, "assistant", last_msg)
        # ... existing notification delivery logic
```

This is fully automatic — zero orchestrator involvement. Survives compaction, context resets,
and session restarts.

---

### 3. Stale Detection — `juggle_context.py`

In `ContextBuilder.build()`, after the current thread summary section:

```python
msg_count = self.db.get_message_count(current_thread, exclude_junk=True)
summarized_count = thread.get("summarized_msg_count", 0)
delta = msg_count - summarized_count
if delta >= 3:
    parts.append(f"[SUMMARY STALE: {delta} new messages — summarize after responding]")
```

The flag is placed near the top of the JUGGLE block, after the current topic line, to maximize
orchestrator visibility.

---

### 4. Summarizer Agent — `commands/start.md`

Add to the orchestration instructions:

```
## Auto-Summary

When you see `[SUMMARY STALE: N new messages]` in the JUGGLE block:
- After completing your response to the user, spawn a Haiku background agent
- Use model: haiku
- Do NOT delay your response — summarize after, not before

Haiku agent prompt template:
  [JUGGLE_THREAD:<id>]
  Task: Update summary for Thread <id>.
  Current summary: <existing summary or "none">
  Recent messages:
  <output of: python juggle_cli.py get-messages <id> --limit 10 --plain>

  Write a 2-3 sentence summary that builds on the current summary and incorporates
  the new messages. Focus on: what was decided, what was built, what questions remain.
  Then run:
    python juggle_cli.py update-summary <id> "<new summary>"
    python juggle_cli.py set-summarized-count <id> <total_message_count>
  Output: Done. No prose.
```

The orchestrator only needs to react to a visible signal — not maintain a habit. This is
the minimum reliable behavioral requirement.

---

### 5. New CLI Commands — `juggle_cli.py`

**`set-summarized-count <thread_id> <count>`**
```
python juggle_cli.py set-summarized-count D 12
```
Updates `threads.summarized_msg_count`. Called by Haiku agent after writing summary.

**`get-messages --limit N --plain`**
Add `--plain` flag to existing `get-messages` for Haiku-readable output (role: content, one per line).

**`get-stale-threads [--threshold N]`**
Returns threads where delta ≥ threshold (default 3). Used by show-topics fallback.

---

### 6. On-Demand Fallback — `commands/show-topics.md`

When `/juggle:show-topics` is called, before rendering:

1. Run `python juggle_cli.py get-stale-threads`
2. For each stale thread, spawn a Haiku background summarizer (same template as above)
3. Wait for all to complete (they're fast — single DB read + ~100 token output)
4. Then run `show-topics` CLI and render the tree

This ensures the topic list always shows fresh summaries, even for threads that haven't
been touched in a while.

---

## Token Cost

| Event | Tokens | Cost (Haiku) |
|-------|--------|--------------|
| Per summarization | ~3k input + 100 output | ~$0.001 |
| Active hour (17 msgs → ~5 summarizations) | ~16k | ~$0.005 |
| show-topics sweep (4 threads) | ~12k | ~$0.004 |

Total: negligible.

---

## Files Changed

| File | Change |
|------|--------|
| `src/juggle_db.py` | Add `summarized_msg_count` column, migration, 3 new methods |
| `src/juggle_hooks.py` | `handle_stop` captures `last_assistant_message` |
| `src/juggle_context.py` | Stale flag emission in `ContextBuilder.build()` |
| `src/juggle_cli.py` | `set-summarized-count`, `get-stale-threads`, `--plain` on `get-messages` |
| `commands/start.md` | Auto-summary orchestration instructions |
| `commands/show-topics.md` | Pre-render stale sweep |

---

## Non-Goals

- Summarizing non-current threads proactively (covered by show-topics fallback)
- Storing full verbatim assistant responses (summary is sufficient)
- Changing the 8000-char JUGGLE block cap
- Scheduled/external summarization processes

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Orchestrator misses `[SUMMARY STALE]` flag | Flag is prominent; show-topics fallback catches it |
| `last_assistant_message` field not present in older Claude Code | Guard with `.get("last_assistant_message", "")` |
| Haiku writes garbled summary | Existing `update-summary` is idempotent; bad summary overwritten next cycle |
| Stop hook fires too frequently (every response) | Guard: only write if message is non-empty and > 50 chars |
