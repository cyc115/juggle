---
name: schedule-dogfood
description: Weekly Saturday 03:00 routine — spawns a headless Juggle research agent to analyze the past 7 days of operational data and writes reports/dogfood-YYYY-MM-DD.md, then files a Juggle action item.
triggers:
  - /schedule:dogfood
  - schedule-dogfood
schedule:
  local: "0 3 * * 6"
  utc: "0 8 * * 6"
  day: "Saturday 03:00 America/Chicago"
---

# /schedule:dogfood

**Goal:** Use Juggle's own research agent infrastructure to analyze the past week's operational data. Produces a self-analysis report that informs Sunday's autofix PR.

## Deliverable

- `~/github/juggle/reports/dogfood-YYYY-MM-DD.md` committed to `main`
- Juggle action item: `type=decision, priority=high` with first suggested improvement

## Commands

```bash
cd ~/github/juggle

# Live run
python3 src/juggle_cli.py schedule-dogfood

# Dry run (no Git/GitHub side effects; report written to /tmp/)
python3 src/juggle_cli.py schedule-dogfood --dry-run
```

The script automatically chooses:
- **Path A** (preferred): Juggle tmux session exists → spawns researcher via `thread create` + `agent get` + `agent send-task`
- **Path B** (fallback): No tmux session → runs `claude -p` headlessly

## Pre-flight checks (automatic)

1. Checks for prior open dogfood thread — skips if found, files action item
2. Checks for active Juggle session — defers 60s and retries once; aborts if still active

## Success criteria

- `reports/dogfood-YYYY-MM-DD.md` exists and contains `## Observed Friction Patterns`
- Juggle action item filed with `type=decision, priority=high`
- Cost < $1.00 (kill-switch fires at $1.00)
- Runtime < 10 minutes (agent timeout at 600s)

## Failure handling

| Failure | Action |
|---------|--------|
| Cost cap ($1.00) exceeded | Write partial report, file `[DOGFOOD-COST-CAP]` action item |
| Agent timeout | Write partial report, file `[DOGFOOD-TIMEOUT]` action item |
| Prior open dogfood thread | Skip run, file action item to resolve prior thread first |
| Active session conflict | Defer 60s, retry once, then abort with action item |
| Zero findings | File `[NO FINDINGS THIS WEEK]` action item (suppress after 3 consecutive weeks) |

## Cross-routine coupling

Autofix (Sunday 03:00, ~24h later) reads the dogfood report at startup and embeds the top 2 suggestions in the PR body. This is read-only — autofix does not reorder its commits based on dogfood.

## agent complete format

```
python3 src/juggle_cli.py agent complete <THREAD_ID> "Dogfood complete: reports/dogfood-YYYY-MM-DD.md written. Cost=$X.XX. Action item filed." --retain "Dogfood ran YYYY-MM-DD. Top finding: <summary>."
```
