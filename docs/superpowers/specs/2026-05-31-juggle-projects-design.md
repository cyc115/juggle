# Juggle Projects — Design Spec
**Date:** 2026-05-31
**Status:** Approved

## Overview

Add a first-class `projects` concept to Juggle. Every topic/thread is assigned to a project. Projects have well-defined objectives and success criteria. A built-in LLM coach helps users define projects clearly. Assignment is fully automatic and asynchronous — users never wait on it.

**Two purposes:**
1. Help the user define goals upfront and stay focused on the long-term objective of a project
2. *(Future)* Autonomous task decomposition and execution toward project completion

---

## Data Model

### New `projects` table

```sql
CREATE TABLE IF NOT EXISTS projects (
  id               TEXT PRIMARY KEY,        -- short label: P1, P2, ... (INBOX is reserved)
  name             TEXT NOT NULL,
  objective        TEXT NOT NULL,           -- one-sentence goal
  success_criteria TEXT NOT NULL DEFAULT '[]',  -- JSON array of verifiable strings
  out_of_scope     TEXT DEFAULT '',         -- optional
  status           TEXT NOT NULL DEFAULT 'active',  -- active|paused|completed|archived
  created_at       TEXT NOT NULL,
  last_active      TEXT NOT NULL
);
```

### `threads` table migration

Add column: `project_id TEXT REFERENCES projects(id) DEFAULT 'INBOX'`

### INBOX project (seeded at migration time)

A real row with `id='INBOX'`, locked — cannot be deleted, edited, or critiqued. All unassigned threads land here. Keeping it as a real row means `project_id` is never NULL — no null-checks scattered through the codebase.

### Migration (via `juggle doctor`)

1. Create `projects` table
2. Seed INBOX row
3. Add `project_id` column to `threads`
4. Backfill all existing threads to `project_id = 'INBOX'`

---

## Project Creation Wizard (LLM Coach)

`juggle project create` launches a conversational LLM coach (Sonnet), not a blank form.

**Flow:**
1. User provides as little as a vague idea (e.g. "automate investing ideas")
2. Coach asks 2–3 targeted questions to surface what "done" looks like
3. Coach proposes a sharpened objective + 2–3 success criteria
4. Coach flags scope issues ("this sounds like 3 projects — want to narrow it?")
5. Coach asks about out-of-scope only if boundaries seem ambiguous
6. Shows drafted project card for user approval:
   ```
   Name: Automated Investing Idea Generation
   Objective: ...
   Success criteria:
     - [ ] ...
     - [ ] ...
   Out of scope: ...
   ```
7. User edits inline or approves → project inserted

**`--force` flag:** bypasses the wizard entirely for users with a pre-written definition.

**Quality bar enforced by coach (not a separate validator):**
- Clarity: objective is unambiguous
- Measurability: success criteria verifiable by a third party
- Scope: achievable vs. open-ended
- Conflict: does not significantly overlap an existing project

---

## Auto-Assignment (Async, Fail-Silent)

Every new thread is assigned a project automatically. This **never blocks** thread creation or any subsequent task execution.

### Flow

```
create-thread inserts thread with project_id = 'INBOX'   ← instant, never blocks
     ↓
threading.Thread(target=_assign_project_bg, args=(db, thread_uuid, topic))
fires immediately after insert
     ↓
background: _cheap_llm_call(prompt) → parse JSON → db.update_thread(project_id=result)
     ↓
cockpit reflects updated project on next refresh cycle
```

### Failure contract

Timeout, LLM error, parse error, DB write error — **all caught, all logged to `juggle-cli.log` only**. Thread stays as INBOX. No error surfaced to user, no notification, no retry, no action item filed. The only observable effect of failure: thread appears under Inbox instead of a named project.

### Implementation

Extracted from existing `title_gen` infrastructure in `juggle_cli_common.py`:

```python
# juggle_cli_common.py
def _cheap_llm_call(prompt: str, timeout: int = 10) -> str | None:
    """OpenRouter (Tier 1) → Haiku subprocess (Tier 2) → None on total failure."""
    # Extracted from _generate_title_for_thread — no DB side-effects

# juggle_cmd_projects.py
def infer_project_id(topic: str, projects: list[dict]) -> str:
    """Pure function. Returns best project_id or 'INBOX'. No DB, no threading."""
    if not projects:
        return "INBOX"
    prompt = (
        f'Topic: "{topic}". '
        f'Projects: {[f"{p[\"id\"]}: {p[\"name\"]} — {p[\"objective\"]}" for p in projects]}. '
        f'Return JSON only: {{"project_id": "<best_id_or_INBOX>"}}. No explanation.'
    )
    raw = _cheap_llm_call(prompt, timeout=5)
    # parse, validate project_id in known ids, else INBOX
    ...

def assign_project_background(db, thread_uuid: str, topic: str) -> None:
    """Thin async wrapper. Fetches projects, fires background thread."""
    ...
```

### Testability requirements (must be verified in spec/plan/implementation)

- [ ] `infer_project_id` has unit tests covering:
  - exact topic→project match
  - fuzzy/semantic match
  - no projects list → returns INBOX
  - LLM returns unknown project_id → returns INBOX
  - LLM call fails/times out → returns INBOX
- [ ] Integration test: create thread, join background assignment thread, assert `thread.project_id != 'INBOX'` when a matching project exists

---

## CLI Commands

New file: `juggle_cmd_projects.py`

| Command | Behavior |
|---|---|
| `juggle project create` | Conversational coach wizard → inserts project |
| `juggle project list` | Table: id, name, status, active thread count |
| `juggle project show <id>` | Full project card + thread list |
| `juggle project assign <thread> <project>` | Manual project override for a thread |
| `juggle project critique <id>` | Re-run coach on existing project (on-demand) |
| `juggle project edit <id>` | Update objective, criteria, or out-of-scope |

---

## Cockpit Changes

Thread list panel grouped by project:

```
▸ INVESTING AUTOMATION          2 active
    [NE] 🟢  Organize Email Inbox
    [NM] ✅  Reddit Adapter

▸ INBOX                         1 active
    [NF] 🟢  Some unassigned topic
```

- Project headers are collapsible
- Sort: active projects first, INBOX always last, archived hidden
- Thread count shown per project header

---

## `juggle:start` Skill Update

The `juggle:start` skill must be updated to document the new `project` subcommands so the orchestrator LLM can invoke them:

```
juggle project create       — define a new project via coach wizard
juggle project list         — list all projects with thread counts
juggle project show <id>    — full project card + threads
juggle project assign <t> <p> — manually assign a thread to a project
juggle project critique <id> — re-run project coach on existing project
juggle project edit <id>    — update project fields
```

---

## Out of Scope (Phase 1)

- Autonomous task decomposition and execution toward project goals (Phase 2)
- Project-level progress metrics or burn-down
- Multi-user project sharing
- Project templates

---

## Success Criteria

- [ ] `juggle project create` wizard guides user from vague idea to well-defined project
- [ ] All new threads are assigned a project asynchronously — no blocking of thread creation
- [ ] Assignment failures are silent — no user-visible error under any failure mode
- [ ] Cockpit thread list groups threads under project headers
- [ ] `juggle doctor` migrates existing DBs cleanly (INBOX backfill)
- [ ] Unit + integration tests for `infer_project_id` and `assign_project_background`
- [ ] `juggle:start` skill updated with project command reference
