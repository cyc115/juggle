---
name: schedule-reflect
description: Weekly Monday 03:00 routine — queries Juggle DB + Hindsight + auto-memory for a weekly digest (reports/reflect-YYYY-MM-DD.md) committed directly to main, plus up to 5 GitHub issues.
triggers:
  - /schedule:reflect
  - schedule-reflect
schedule:
  local: "0 3 * * 1"
  utc: "0 8 * * 1"
  day: "Monday 03:00 America/Chicago"
---

# /schedule:reflect

**Goal:** Produce a weekly operational digest covering watchdog health, action item patterns, agent quality, context bloat, memory health, and skill drift. Read-only — no code changes, no edits outside `~/github/juggle/`.

## Deliverable

- `~/github/juggle/reports/reflect-YYYY-MM-DD.md` committed directly to `main`
- Up to 5 GitHub issues (label: `routine-reflect`) for actionable findings
- No edits to files outside `~/github/juggle/`

## Commands

```bash
cd ~/github/juggle

# Live run (commits digest, files issues)
uv run src/juggle_cli.py schedule-reflect

# Dry run (writes digest to /tmp/schedule-reflect-sample-digest.md, no Git/GitHub)
uv run src/juggle_cli.py schedule-reflect --dry-run
```

## Digest sections

| ID | Source | Section title |
|----|--------|--------------|
| RF-1 | watchdog_events (7d) | Watchdog Health |
| RF-2 | action_items (30d) | Action Item Fatigue |
| RF-3 | agent_completions (7d) | Agent Output Quality |
| RF-4 | messages table (7d) | Context Bloat Candidates |
| RF-5 | Hindsight API (60d+ old) | Memory Health |
| RF-6 | ~/.claude/.../memory/ | Auto-Memory Contradictions |
| RF-7 | skill descriptions vs DB tasks | Skill Drift |
| RF-8 | Most recent dogfood report | Dogfood Pulse |

All sections attempted regardless of failures — partial digest beats no digest.

## Issue filing rules

- Max 5 issues per run
- Dedup: skip if matching title exists within 30 days
- Priority: RF-1 > RF-7 > RF-2 > RF-5 > RF-8
- Label: `routine-reflect` (auto-created on first use)
- Title format: `reflect: <summary>` (max 72 chars)

## Success criteria

- `reports/reflect-YYYY-MM-DD.md` committed to main
- All 8 sections present (partial sections marked, not absent)
- ≤5 new issues with `routine-reflect` label
- No files modified outside `~/github/juggle/`
- Cost < $2.00

## Failure handling

| Failure | Action |
|---------|--------|
| Hindsight unavailable | Skip RF-5, mark "Hindsight unavailable" in digest |
| auto-memory path not found | Skip RF-6, mark in digest |
| DB query returns 0 rows | Write "No events this week" — positive signal |
| Cost cap ($2.00) | Write partial digest with note, stop additional LLM calls |
| GitHub rate limit | Log remaining issues in digest, retry at next run |

## complete-agent format

```
uv run src/juggle_cli.py complete-agent <THREAD_ID> "Reflect complete: reports/reflect-YYYY-MM-DD.md committed. N issues filed. Cost=$X.XX." --retain "Reflect ran YYYY-MM-DD. Key finding: <summary>."
```
