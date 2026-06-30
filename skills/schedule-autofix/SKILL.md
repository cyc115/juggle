---
name: schedule-autofix
description: Weekly Sunday 03:00 routine — runs automated code fixes (ruff, vulture, test generation, doc drift, CHANGELOG, graphify) on a PR branch cyc_schedule-autofix-YYYY-MM-DD for human review before merge.
triggers:
  - /schedule:autofix
  - schedule-autofix
schedule:
  local: "0 3 * * 0"
  utc: "0 8 * * 0"
  day: "Sunday 03:00 America/Chicago"
---

# /schedule:autofix

**Goal:** Run automated code-quality fixes in a PR branch for human review. No auto-merge — human must approve before merging.

## Deliverable

- PR `cyc_schedule-autofix-YYYY-MM-DD` → `main`
- GitHub issues for security findings (IS-1), skill retirement candidates (IS-2), low-confidence dead code (IS-3/FX-2 <95%)
- PR opened but NOT merged — human review gate is mandatory

## Commands

```bash
cd ~/github/juggle

# Live run (creates branch, commits fixes, opens PR)
python3 src/juggle_cli.py schedule-autofix

# Dry run (runs analysis, writes would-be PR to /tmp/schedule-autofix-sample-PR.md)
python3 src/juggle_cli.py schedule-autofix --dry-run
```

## Fix sections (each = one commit on branch)

| ID | What | Safety gate |
|----|------|-------------|
| FX-1 | ruff --fix (F401, F841, E501) | Always committed |
| FX-2 | vulture ≥95% confidence dead code | grep confirms 0 live refs |
| FX-3 | LLM-generated test gaps → `tests/auto-generated/` | Failing cases get `@pytest.mark.skip` |
| FX-4 | Watchdog regression tests from events | Same skip gate as FX-3 |
| FX-5 | Doc drift corrections (code is truth) | Diff appended verbatim to PR body |
| FX-6 | CHANGELOG entry from `git log --since=7 days ago` | Always committed |
| FX-7 | `graphify update .` refresh | Deterministic, no LLM cost |

## Out-of-PR issues

| ID | Title format |
|----|-------------|
| IS-1 | `autofix: security finding — <severity> in <file>:<line>` |
| IS-2 | `autofix: skill retirement candidate — <skill> (0 invocations, 30d)` |
| IS-3 | `autofix: probable dead code — <function> (<confidence>%)` |

## Success criteria

- PR `cyc_schedule-autofix-YYYY-MM-DD` created in juggle repo
- PR contains ≥3 of 7 fix sections (not all will have findings every week)
- `pytest src/ tests/` (excluding auto-generated) passes on branch
- PR body includes cross-link to dogfood report if one exists from past 48h
- Cost < $2.00

## Failure handling

| Failure | Action |
|---------|--------|
| Existing autofix PR still open | Skip run, file Juggle action item to close/merge first |
| `git push` fails | File action item, delete partial branch, return to main |
| Routine times out | Push whatever exists, open PR with `[PARTIAL]` in title |
| Smoke test fails | Revert offending commit, mark `[REVERTED: <section>]` in PR body |
| Tool unavailable (ruff/bandit) | Mark section "tool unavailable", continue |

## agent complete format

```
python3 src/juggle_cli.py agent complete <THREAD_ID> "Autofix complete: PR cyc_schedule-autofix-YYYY-MM-DD created. N sections committed. Cost=$X.XX." --retain "Autofix ran YYYY-MM-DD. PR: <URL>."
```
