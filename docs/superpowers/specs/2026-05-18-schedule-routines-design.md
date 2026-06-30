# Design Spec: `/schedule:autofix`, `/schedule:reflect`, and `/schedule:dogfood`

**Date:** 2026-05-18  
**Status:** Draft — awaiting Routines API verification  
**Author:** JI research agent  
**Scope:** Three new Juggle `/schedule:*` skills backed by Claude Code Routines (cloud-managed, April 2026)

---

## 1. Background & Motivation

Juggle accumulates technical debt, stale documentation, and operational telemetry that is never systematically reviewed. Existing improvement work is ad-hoc — triggered by visible breakage, not proactive analysis. Two properties make automated scheduled work tractable now:

1. **Juggle's SQLite DB is rich telemetry.** `watchdog_events`, `action_items`, `agent_completions`, and `threads` tables contain a week's worth of behavioral signal that nothing currently mines.
2. **Claude Code Routines (April 2026)** provide cloud-managed scheduled execution without requiring a local cron daemon, launchd entry, or GitHub Actions pipeline.

The goal: three weekly automated jobs that run unsupervised, produce concrete artifacts (a PR, a digest report, GitHub issues), and require human review only when something warrants it.

**Schedule rationale:** All three routines run between 03:00–05:00 local time on Saturday, Sunday, and Monday respectively. This window consumes unused Claude Code subscription credit limits during off-peak hours when the account would otherwise be idle.

**Research basis:**
- JA chain: ruff → vulture → radon cc → git churn → claude -p synthesis (2026-05-18-refactor-agents-for-scheduled-juggle.md)
- JB catalog: Categories A–F of schedulable work with confidence/cost/runtime estimates (2026-05-18-schedulable-juggle-self-improvement.md)

---

## 2. Locked Decisions

The following decisions are settled and not re-debated in this spec:

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Three routines, not one** | Autofix (code changes), reflection (operational digest), and dogfood (meta-analysis) have incompatible cost profiles and output mechanisms. Bundling them creates a single point of failure and obscures billing. |
| 2 | **Autofix → ONE omnibus PR per week** | Avoids PR spam. Human reviews one thing. Risky findings that don't belong in a PR go to GitHub issues instead. |
| 3 | **Reflect → weekly digest doc + GitHub issues** | Read-only. No code changes. Issues are actionable items that persist beyond the digest doc's lifespan. F2 (dogfood) removed from reflect — it is now Routine 3. |
| 4 | **Dogfood → own routine (separate from reflect)** | Different cost profile (Sonnet + ~5 min vs Haiku + <1 min), different output mechanism (spawns real Juggle agent + action item), meta-recursive identity. Runs Saturday 03:00; findings inform Sunday's autofix PR. |
| 5 | **Execution layer → Claude Code Routines** | Cloud-managed. No local daemon. Survives laptop sleep. No GitHub Actions infrastructure overhead. |
| 6 | **PR exception override** | Juggle's project convention is commit-directly-to-main. These routines **override that convention** because they run unsupervised. PRs are the mandatory human review gate. See §6.4. |

---

## 3. Routine 1: `/schedule:autofix`

### 3.1 Overview

| Property | Value |
|----------|-------|
| Schedule | Sunday 03:00 local time — `0 3 * * 0` |
| Primary deliverable | One omnibus PR: branch `cyc_schedule-autofix-YYYY-MM-DD` → `main` |
| Secondary deliverables | GitHub issues for risky or judgment-call findings |
| Side effects | None outside the juggle repo |
| Estimated runtime | 8–12 minutes |
| Estimated cost | ~$0.20–0.40/run (JA chain + LLM test generation + doc drift) |

### 3.2 In-PR Auto-Fix Table

Each row is one commit on the branch. If the section produces no diff, it is marked "no findings this week" in the PR body — it does not block the PR.

| ID | Source | What gets committed | Safety gate |
|----|--------|---------------------|-------------|
| FX-1 | JA: ruff --fix | Lint fixes: unused imports (F401), unused vars (F841), line length (E501) | Battle-tested; always commit |
| FX-2 | JA: vulture ≥95% confidence | Remove dead functions/classes/vars with confidence ≥95% | Grep confirms zero live references in `src/` AND `tests/` before removal; anything <95% → issue, not commit |
| FX-3 | A1: test coverage gaps | `tests/auto-generated/YYYY-MM-DD-gaps.py` — LLM-generated pytest cases for 0%-covered functions | Run `pytest tests/auto-generated/` on the branch; any failing case gets `@pytest.mark.skip(reason="auto-generated, needs review")` and is tagged in PR body |
| FX-4 | F1: watchdog regression tests | `tests/auto-generated/watchdog-regression-YYYY-MM-DD.py` — LLM-generated cases from `watchdog_events` snapshots | Same skip-on-fail gate as FX-3 |
| FX-5 | B1: spec vs code drift | LLM rewrites stale doc sections to match code (code is source of truth) | Doc diff appended verbatim to PR description in a collapsible `<details>` block; human verifies intent wasn't corrupted |
| FX-6 | B2: CHANGELOG | Append weekly entry from `git log --since="7 days ago" --oneline --no-merges` | Always safe — pure generative append |
| FX-7 | E1: graphify refresh | `graphify update .` regenerates `graphify-out/*` | Pure deterministic regeneration; no LLM cost |

### 3.3 Out-of-PR GitHub Issues Table

| ID | Source | Issue title format | Why issue, not PR |
|----|--------|--------------------|-------------------|
| IS-1 | A3: bandit | `autofix: security finding — <severity> in <file>:<line>` | Security findings need human triage; LLM should not auto-remove |
| IS-2 | B3: skill audit | `autofix: skill retirement candidate — <skill_name> (0 invocations, 30d)` | Retirement is a judgment call; some skills are infrequent by design |
| IS-3 | JA: vulture <95% | `autofix: probable dead code — <function> in <file> (<confidence>%)` | Risk of removing live code accessed via dynamic dispatch or external callers |

### 3.4 PR Description Schema

```markdown
## autofix: YYYY-MM-DD

> Generated by `/schedule:autofix` via Claude Code Routines.
> Branch: `cyc_schedule-autofix-YYYY-MM-DD`

### Summary

| Fix | Files changed | Lines +/- | Status |
|-----|--------------|-----------|--------|
| FX-1 ruff | N | +0/-M | ✅ committed |
| FX-2 vulture | N | +0/-M | ✅ committed / ⚪ no findings |
| FX-3 test gaps | 1 | +N/-0 | ✅ committed / ⚠️ N cases skipped |
| FX-4 watchdog tests | 1 | +N/-0 | ✅ committed / ⚠️ N cases skipped |
| FX-5 doc drift | N | +N/-N | ✅ committed / ⚠️ see diff below |
| FX-6 CHANGELOG | 1 | +N/-0 | ✅ committed |
| FX-7 graphify | varies | varies | ✅ refreshed |

### Related issues filed this run
- #NNN: <issue title>

### Cross-routine link
Reflect digest from last Monday: `reports/reflect-YYYY-MM-DD.md` (or "not yet run this week")

### Doc drift details
<details><summary>Expand doc diffs</summary>
...
</details>

### [CRITIQUE] Flagged by LLM as potentially risky
- [ ] <describe any change the LLM flagged but committed anyway>

### [PARTIAL] (if routine timed out)
Completed sections: FX-1, FX-2. Incomplete: FX-3 onwards.
```

### 3.5 Failure Modes

| Failure | Detection | Response |
|---------|-----------|----------|
| Previous week's PR still open | `gh pr list --head "cyc_schedule-autofix-"` returns results | Skip this run entirely; file Juggle action item: "autofix PR from YYYY-MM-DD still open — review or close before next run" |
| `git push` fails (auth, conflict) | Non-zero exit from `git push` | File Juggle action item: "autofix push failed: <error>"; do NOT leave partial branch in ambiguous state — run `git branch -D cyc_schedule-autofix-YYYY-MM-DD` to clean up |
| Routine times out mid-fix | Routine execution limit reached | Push whatever commits exist; open PR with `[PARTIAL]` tag in title; list completed vs incomplete sections |
| LLM produces no diff for a section | Empty output / no file changes | Mark section "no findings this week" in PR body; do not block PR or file issue |
| `pytest` smoke fails on PR branch | `pytest src/ tests/` (excluding auto-generated) returns non-zero | Identify which commit caused regression via `git bisect` or last-fix rollback; revert that commit; mark `[REVERTED: <section>]` in PR body; continue with remaining sections |
| Bandit / ruff / vulture not installed | Command not found | Install via `uvx` on first use; if unavailable, mark section as "tool unavailable" and continue |
| No watchdog snapshots exist | `watchdog_events` query returns empty | Mark FX-4 "no stall events this week — no regression tests generated"; not an error |

---

## 4. Routine 2: `/schedule:reflect`

### 4.1 Overview

| Property | Value |
|----------|-------|
| Schedule | Monday 03:00 local time — `0 3 * * 1` |
| Primary deliverable | `~/github/juggle/reports/reflect-YYYY-MM-DD.md` committed to main |
| Secondary deliverables | Up to 5 GitHub issues for actionable findings |
| Side effects | Hindsight API queries (read-only); no edits outside juggle repo |
| Estimated runtime | 10–15 minutes (F2 dogfood dispatch dominates) |
| Estimated cost | ~$0.35–0.60/run (F2 Sonnet dominates: ~$0.20–0.30; others Haiku) |

### 4.2 Digest Sections Table

Each section is one query+analysis step. All sections are attempted regardless of prior section outcome — partial digests are better than no digest.

| ID | Source | Query / analysis | Output in digest |
|----|--------|-----------------|-----------------|
| RF-1 | C1 watchdog telemetry | `watchdog_events` last 7 days → LLM: top 5 failure modes, re-dispatch success rate, threshold tuning suggestions | Section: "Watchdog Health" |
| RF-2 | C2 action item ack patterns | `action_items` last 30 days: type × avg-days-to-ack × dismiss-without-followup rate → LLM analysis | Section: "Action Item Fatigue" with keyword tuning suggestions |
| RF-3 | C3 thread completion quality | `agent_completions` last 7 days: result_summary batch → LLM rates 1–5 completeness | Section: "Agent Output Quality" with distribution + outlier list |
| RF-4 | C4 token/message outliers | `messages` table: thread_id × msg_count, last 7 days → top 5 by msg_count | Section: "Context Bloat Candidates" |
| RF-5 | D1 Hindsight memory lint | Hindsight entries >60 days old → LLM: contradictions, stale code refs, duplicates | Section: "Memory Health" with archive/merge suggestions |
| RF-6 | D2 auto-memory scan | Files in `~/.claude/projects/.../memory/` → LLM: contradiction + staleness check | Section: "Auto-Memory Contradictions" — **suggestions only, no edits** (outside juggle repo) |
| RF-7 | F3 skill description drift | Skill `description:` frontmatter vs. agent task prompts in DB → LLM match scoring | Section: "Skill Drift" — skills whose invocation pattern diverges from description |
| RF-8 | Dogfood cross-link | Read most recent `reports/dogfood-*.md`; embed 1-paragraph summary + action item resolution rate since last week | Section: "Dogfood Pulse" — how many dogfood action items were ack'd this week |

> **Note:** F2 (dogfood dispatch) was removed from this list and promoted to its own routine (`/schedule:dogfood`, §5). Reflect now cross-links to dogfood findings rather than running the agent itself.

### 4.3 Cross-Routine Awareness

RF-1 through RF-4 should reference the autofix PR from earlier in the week if it exists:

```
> This digest covers 2026-MM-DD through 2026-MM-DD.
> Autofix PR this week: #NNN (cyc_schedule-autofix-YYYY-MM-DD) — merged/open/not run.
```

If autofix PR is open and has `[REVERTED]` sections, RF-1 should note which auto-fixes were rolled back.

### 4.4 GitHub Issue Gating

- Cap: **5 new issues maximum per reflect run**
- Dedup: skip issue creation if a `gh issue list --state all --search "reflect: <title>"` result exists with matching title created within 30 days
- Title format: `reflect: <finding summary>` (max 72 chars)
- Body: relevant excerpt from the digest section + link to full report
- Label: `routine-reflect` (auto-created on first use)
- Priority selection: prefer RF-1 (watchdog) > RF-7 (dogfood) > RF-2 (action items) > RF-5 (memory) > RF-8 (skill drift) when capping at 5

### 4.5 Failure Modes

| Failure | Response |
|---------|----------|
| Hindsight API unavailable | Skip RF-5; mark "Hindsight unavailable this week" in digest |
| `~/.claude/projects/.../memory/` path not found | Skip RF-6; mark in digest |
| F2 dogfood dispatch times out or returns no result | Mark RF-7 "dispatch timed out"; do not block digest |
| DB query returns 0 rows (e.g., no watchdog events in a quiet week) | Write "No events this week" for that section; positive signal, not an error |
| Issue creation hits GitHub rate limit | Log remaining unwritten issues in digest body; attempt at next run |

---

## 5. Routine 3: `/schedule:dogfood`

### 5.1 Overview

| Property | Value |
|----------|-------|
| Schedule | Saturday 03:00 local time — `0 3 * * 6` |
| Purpose | Use Juggle's own research agent infrastructure to analyze the past week's operational data |
| Primary deliverable | `~/github/juggle/reports/dogfood-YYYY-MM-DD.md` committed to main |
| Secondary deliverable | Juggle action item: `--type decision --priority high` with summary |
| Cross-routine coupling | Autofix (Mon 03:00) reads most recent dogfood report at startup to weight PR priorities |
| Estimated runtime | ~5–8 minutes (full research agent run) |
| Estimated cost | ~$0.20–0.35/run (Sonnet) |

### 5.2 Research Agent Invocation

The routine invokes a headless Juggle research agent via the CLI in cockpit-less mode (no tmux UI required):

```
juggle_cli.py thread create --topic "dogfood-YYYY-MM-DD" --domain juggle
juggle_cli.py get-agent --thread <id> --role researcher
# task injected via send-task:
juggle_cli.py send-task <agent_id> <task_file>
```

**Task prompt (auto-populated):**
> "Review last 7 days of completed threads in juggle.db. Focus only on threads created after YYYY-MM-DD. What patterns of user friction, repeated dispatches, blockers, or unresolved open questions do you observe? Suggest 1–3 concrete Juggle improvements with file:line refs where applicable. Do not reference dogfood reports older than 4 weeks — use only fresh data."

The 4-week freshness window prevents the confirmation bias loop described in §8.

### 5.3 Headless Mode — No Live Juggle Required

The routine must work even when the user's Juggle session is not running (cockpit not open, tmux session may not exist).

**Path A (preferred):** If the juggle tmux session exists, use normal `get-agent` → `send-task` flow. The researcher runs in an existing pane.

**Path B (fallback):** If no juggle tmux session exists, invoke Claude headlessly:
```bash
claude -p "<task prompt>" --output-format json \
  --allowedTools "Read,Bash,mcp__juggle__*"
```
This bypasses tmux entirely. The output is captured and written to the dogfood report file directly without going through the DB-backed thread lifecycle.

**Which path is used:** The routine detects session existence via `tmux has-session -t juggle 2>/dev/null`. If it returns non-zero, fall back to Path B.

### 5.4 Conflict with Live Session

If Juggle is actively in use when the routine fires (Saturday 03:00 is consistently off-peak, but conflicts are still possible):

- The routine checks `db.get_current_thread()` — if a thread is actively focused and the user has been active in the last 30 minutes, defer by 60 minutes and retry once.
- On the retry, if Juggle is still in active use, abort and file action item: `"Dogfood routine deferred — Juggle in active use at Saturday 03:00. Run manually: juggle-dogfood"`
- The routine does NOT interrupt or interfere with the live session.

### 5.5 Prior Week's Thread Conflict

If a dogfood research thread from a prior week is still open (status ≠ closed):

- Check `SELECT id, status FROM threads WHERE topic LIKE 'dogfood-%' AND status NOT IN ('closed','archived','failed')` 
- If any exist: skip spawning a new agent; file action item: `"Prior dogfood thread <label> still unresolved — review before this week's run"`
- Do NOT supersede or close the prior thread — the user may be mid-review

### 5.6 Output Format

**`reports/dogfood-YYYY-MM-DD.md`:**
```markdown
# Juggle Self-Analysis — YYYY-MM-DD

> Generated by `/schedule:dogfood` via Claude Code Routines.
> Data window: YYYY-MM-DD to YYYY-MM-DD (7 days)
> Agent: researcher, model: claude-sonnet-4-6

## Observed Friction Patterns
1. ...

## Repeated Dispatches / Blockers
...

## Unresolved Open Questions
...

## Suggested Improvements (1–3)
1. **<title>** — <description>. See `src/<file>.py:<line>`.
...

## Raw thread summary (for archival)
<agent result_summary text>
```

**Juggle action item filed on completion:**
- Message: `"Dogfood findings: <first suggested improvement, truncated to 120 chars>"`
- Type: `decision`
- Priority: `high`
- If result is empty / no suggestions: `"[NO FINDINGS THIS WEEK] Dogfood ran successfully but found no actionable improvements"`

### 5.7 Cross-Routine Coupling: Dogfood → Autofix

Autofix (Mon 03:00, ~7 hours later) reads the dogfood report at startup:

```python
latest_dogfood = max(glob("reports/dogfood-*.md"), key=os.path.getmtime, default=None)
```

If a dogfood report exists from within the last 48 hours, its "Suggested Improvements" section is embedded in the PR description:

```markdown
### Dogfood findings (from Saturday's analysis)
> <first 2 suggestions from dogfood report>
> Full report: reports/dogfood-YYYY-MM-DD.md
```

This is read-only coupling — autofix does NOT reorder its fix commits based on dogfood findings. The dogfood section is purely informational in the PR body.

### 5.8 Cost Cap and Time Limit

| Limit | Value | Action on breach |
|-------|-------|-----------------|
| LLM cost | $1.00/run | Kill agent, write partial findings to report, file action item `[DOGFOOD-COST-CAP]` |
| Wall time | 10 minutes | Kill agent (watchdog handles this naturally), write partial findings, file action item `[DOGFOOD-TIMEOUT]` |
| Empty output (3 consecutive weeks) | — | Suppress action item, add note to reflect digest: "Dogfood: no findings 3 weeks running — consider pausing" |

### 5.9 Failure Modes

| Failure | Response |
|---------|----------|
| Researcher hangs / stalls | Watchdog kills it (normal watchdog flow); action item filed with `[DOGFOOD-FAILED]` tag; report written as partial |
| Researcher produces zero useful findings | File action item with `[NO FINDINGS THIS WEEK]` tag; write minimal report with "no actionable improvements identified" |
| DB lock contention (live session in use) | Use SQLite WAL mode (`PRAGMA journal_mode=WAL`); retry query up to 3× with 5s backoff; if still locked, skip and note in report |
| `juggle_cli.py` not on PATH in Routines env | Fail fast; file action item: "Dogfood routine: juggle_cli.py not found — check Routines PATH config" |
| 3 consecutive empty-findings weeks | After 3rd week, suppress action item (reduce noise); add flag to reflect digest; do NOT auto-disable routine |

---

## 6. Shared Infrastructure

### 6.1 Claude Code Routines — API & Config

> **⚠️ Open question (see §6.1):** The exact config file format and Routines API spec require one-time verification against current Anthropic docs. The design below is based on the April 2026 launch description; verify before implementation.

Based on available documentation, a Routine is a saved Claude Code configuration consisting of:
- A **task prompt** describing what the agent should do
- One or more **repositories** the routine has access to
- A **trigger** (scheduled cron, API webhook, or GitHub event)
- A set of **connectors** (credentials, tool permissions)

Expected config location (unverified — verify against docs):
```
~/github/juggle/.claude/routines/
  autofix.json
  reflect.json
```

Conceptual schema:
```json
{
  "name": "juggle-autofix",
  "prompt": "...",
  "repositories": ["github.com/mikechen/juggle"],
  "trigger": {
    "type": "schedule",
    "cron": "0 3 * * 0",
    "timezone": "America/Chicago"
  },
  "connectors": {
    "github": { "auth": "app" },
    "anthropic": { "model": "claude-sonnet-4-6" }
  }
}
```

> **UTC conversion note:** If the Routines API requires UTC cron expressions instead of a named timezone, subtract 5h during CDT (May–Nov) or 6h during CST (Nov–May). Examples: Sat 03:00 CDT = `0 8 * * 6` UTC; Sun 03:00 CDT = `0 8 * * 0` UTC; Mon 03:00 CDT = `0 8 * * 1` UTC. Verify the API's timezone support before deploying.

### 6.2 Authentication

| Resource | Method | Notes |
|----------|--------|-------|
| GitHub push + PR creation | GitHub App (recommended) or PAT | App preferred: scoped to juggle repo only, no personal token expiry risk. PAT acceptable for initial setup. Store in Routine connector config, NOT in repo. |
| Anthropic API (LLM calls) | Scoped via Routine's built-in API key | Billed to the account that owns the Routine. Not a separate key. |
| Hindsight API (reflect only) | Existing Hindsight URL + token from juggle settings | Read-only access for reflect; write access not needed. |
| `juggle.db` | Local file access | Routines run in cloud, so DB access requires either: (a) checked-in export (bad), (b) DB exposed via API, or (c) Routine runs a script that queries DB over SSH. **This is a critical open question — see §6.2.** |

### 6.3 Observability

| Signal | Location | How to check |
|--------|----------|-------------|
| Routine execution log | Anthropic Routines dashboard (cloud) | Visit dashboard; each run shows start time, exit status, stdout/stderr |
| Autofix PR | `gh pr list --search "cyc_schedule-autofix"` | CLI check; also visible in GitHub UI |
| Reflect digest | `ls ~/github/juggle/reports/reflect-*.md` | File presence = run succeeded |
| Filed issues | `gh issue list --label routine-reflect` | Issue count + recency |
| Dead-routine detection | See §7.1 (DA finding) | Absence of PR + no digest file for >14 days → Juggle action item |

**Proposed dead-routine canary:** A lightweight local cron (or Juggle hook) runs every Monday morning and checks: "was a new `cyc_schedule-autofix-*` branch or PR created since Sunday?" If not, files a Juggle action item: `⚠️ autofix routine appears dead — no PR created this week`. Similar check for reflect canary on Tuesday.

### 6.4 PR Exception Override (Explicit)

> **This is a formal override of Juggle's project convention.**

Juggle's standard development policy (documented in CLAUDE.md and project memory) is: **commit directly to `main`, no feature branches, no PRs**. This policy exists because Juggle is a single-developer project where PRs add overhead without review benefit.

**These two routines override that policy unconditionally.**

Rationale: the routines run without a human in the loop. A bad auto-fix committed directly to `main` could break Juggle mid-session with no recovery path. PRs provide:
1. A diff to review before merging
2. A `[REVERTED]` or `[PARTIAL]` signal if the routine caught its own mistake
3. A merge gate where the user can close without merging if the run looks wrong

**Branch naming:** `cyc_schedule-autofix-YYYY-MM-DD` (follows existing `cyc_*` feature branch convention).

**Reflect routine** does commit directly to `main` for the digest file (`reports/reflect-YYYY-MM-DD.md`) since it makes no code changes — only additive markdown. The PR exception applies only to Routine 1 (autofix).

### 6.5 Routine Ordering and Cross-Routine Awareness

The three routines form a pipeline with explicit handoffs:

```
Saturday 03:00  /schedule:dogfood
    └─ writes:  reports/dogfood-YYYY-MM-DD.md
    └─ files:   Juggle action item (decision, high)

Sunday 03:00    /schedule:autofix   (~24 hours later)
    └─ reads:   reports/dogfood-YYYY-MM-DD.md → embeds top findings in PR body
    └─ writes:  cyc_schedule-autofix-YYYY-MM-DD branch + PR
    └─ files:   GitHub issues (autofix:)

Monday 03:00    /schedule:reflect   (~24 hours later)
    └─ reads:   reports/dogfood-YYYY-MM-DD.md → RF-8 "Dogfood Pulse"
    └─ reads:   cyc_schedule-autofix-YYYY-MM-DD PR → cross-link in digest header
    └─ writes:  reports/reflect-YYYY-MM-DD.md committed to main
    └─ files:   GitHub issues (routine-reflect:, cap 5)
```

**Handoff contracts:**
- Dogfood → Autofix: dogfood report MUST exist at `reports/dogfood-YYYY-MM-DD.md` (from the Saturday within the past 48h). If absent, autofix skips the cross-link section without failing.
- Autofix → Reflect: reflect looks for the most recent `cyc_schedule-autofix-*` PR regardless of state (open/merged/closed). It cross-links whatever it finds.
- Reflect has no handoff to future routines — it is the end of the weekly cycle.

### 6.6 Dry-Run Mode

Both skills expose a `--dry-run` flag for local testing:

- **autofix --dry-run:** Runs all analysis steps, writes the would-be PR diff to `/tmp/autofix-dryrun-YYYY-MM-DD/`, but does NOT create a branch, does NOT push, does NOT create issues. Prints "DRY RUN: would create PR with N commits" to stdout.
- **reflect --dry-run:** Runs all analysis, writes digest to `/tmp/reflect-dryrun-YYYY-MM-DD.md` but does NOT commit, does NOT create issues.
- **dogfood --dry-run:** Runs the research agent, writes report to `/tmp/dogfood-dryrun-YYYY-MM-DD.md`, does NOT commit, does NOT file action item.

Dry-run uses the real DB and real LLM calls — it simulates execution without Git/GitHub side effects.

### 6.7 Cost Cap / Kill Switch

| Routine | Expected cost | Hard cap |
|---------|--------------|----------|
| autofix | ~$0.20–0.40/run | $2.00/run — if exceeded, Routine exits early, files GitHub issue `autofix: cost cap exceeded`, marks PR `[PARTIAL]` |
| reflect | ~$0.35–0.60/run | $2.00/run — same: exit early, write partial digest with note |
| dogfood | ~$0.20–0.35/run | $1.00/run — tighter cap; single research agent; if exceeded, write partial findings, file action item `[DOGFOOD-COST-CAP]` |

Implementation: track cumulative API spend via `--output-format json` cost fields; abort when cap reached. The caps are ~4–5× expected cost — generous enough to not trigger on normal variance, tight enough to catch runaway loops.

**Annual cost at expected rate:** autofix ~$20/year, reflect ~$25/year, dogfood ~$14/year. **Total: ~$59/year (~$5/month).** Well within acceptable budget.

---

## 7. Open Questions

These require one-time verification before implementation begins.

### 7.1 Claude Code Routines API — exact interface
Routines launched April 2026. The config file location, schema, and authentication connector format need verification against current Anthropic docs at `code.claude.com/docs/en/routines`. Specifically:
- Does the config live in `.claude/routines/` or in the Anthropic cloud dashboard only?
- Is there a CLI command to create/list/delete routines (`claude routine create`)?
- What are the execution environment constraints (filesystem access, network, env vars)?

### 7.2 `juggle.db` access from cloud Routines
The reflect routine needs to query `juggle.db`, which lives on the local machine. Cloud Routines run on Anthropic infrastructure with access to the GitHub repo — but NOT to the local filesystem. Options:
- **Option A:** Export a weekly DB snapshot to the repo (bad: sensitive operational data in git)
- **Option B:** Expose a read-only query endpoint from the local machine (complex: requires port exposure)
- **Option C:** Run reflect as a local script invoked by a thin Routine trigger (Routine fires → sends webhook → local handler runs reflect). Hybrid model.
- **Option D:** Store telemetry in a cloud-accessible location (e.g., a read-only SQLite file synced to a private S3 bucket or encrypted Gist)

**Recommendation pending verification:** Option C (thin-trigger, local execution) preserves all the Routines scheduling benefits while avoiding DB access problems. If Routines supports running scripts in the connected repo's environment, that may resolve this naturally.

### 7.3 GitHub auth — App vs PAT
Does Routine's GitHub connector support GitHub Apps, or only PATs? App is preferred (non-expiring, repo-scoped). If only PAT: set a calendar reminder to rotate the token before expiry.

### 7.4 Routines cost billing
Is Routine API usage billed to the account's standard token budget, or is there a separate Routines SKU? Affects cost cap calculation.

### 7.5 Watchdog snapshot path for F1/FX-4
Research note from JB: "Verified that `juggle_watchdog.py:224` saves snapshots, but path variable not captured." Verify exact path (likely `<config_dir>/watchdog/recovery/`) before implementing FX-4.

### 7.6 Claude Code session JSONL format for B3/IS-2
Skill invocation audit (B3) assumes `~/.claude/projects/**/*.jsonl`. Verify format with a one-time `ls` + `head` before building the parser.

---

## 8. Devil's Advocate

### 8.1 Routine failure cascade — silent dead routines
**Risk:** If a Routine fails silently (auth expires, Anthropic infrastructure issue, bad prompt), the user gets no signal. A dead routine that ran successfully for 3 months could stop working and go unnoticed for weeks.

**Mitigations:**
1. **Cloud alerting (primary):** Anthropic Routines likely has built-in failure notification — verify in docs. If available, configure email/webhook on any failed run.
2. **Canary check (local fallback):** A lightweight local cron (separate from Routines) runs each Monday and Tuesday, checking for expected artifacts (PR existence / digest file freshness). Files a Juggle action item if missing. This is cheap (~2s, no LLM) and provides local redundancy against cloud failure.
3. **Reflect digest freshness:** `reports/reflect-YYYY-MM-DD.md` has a predictable naming pattern. If the most recent file is >10 days old at digest time, the routine prepends a warning to the digest.

**Residual risk:** If BOTH the cloud alerting AND the canary check fail simultaneously, the user goes dark. Acceptable given the low stakes of these routines (missing a weekly PR/digest is inconvenient, not catastrophic).

### 8.2 Bad auto-fix merges silently
**Risk:** A user who merges the autofix PR without reading it could introduce a subtle regression. The routine's smoke test (`pytest src/ tests/`) catches obvious regressions but not subtle behavioral changes (e.g., removing a "dead" function that's actually called dynamically).

**Mitigations:**
1. **95% confidence threshold for FX-2 (vulture)** — only removes code vulture is nearly certain about. Anything below goes to issue.
2. **Smoke test + [REVERTED] tagging** — the routine runs `pytest` on the branch before opening the PR. If smoke fails, the offending commit is reverted and tagged.
3. **`[CRITIQUE]` section** — the LLM explicitly flags anything it's uncertain about, visible in the PR description.
4. **PR-not-auto-merged** — the routine OPENS the PR but never merges it. Human merge is always required.

**Residual risk:** Dynamic dispatch (`getattr`, plugin hooks, CLI command routing via string) can fool static analysis. A `whitelist.py` for vulture should be created and maintained to suppress known-dynamic symbols. Document this in the implementation plan.

### 8.3 Issue spam with weak dedup
**Risk:** Reflect runs weekly. Each run could file 5 issues. After 10 weeks, 50 issues exist. If dedup logic is wrong (e.g., title matching is too loose), issues pile up.

**Mitigations:**
1. **Hard cap of 5 issues per run** — regardless of how many findings exist, only the top 5 get issues.
2. **30-day dedup window with exact title match** — `gh issue list --search "reflect: <exact title>"` before creating. Same finding doesn't get re-filed for a month.
3. **`routine-reflect` label** — makes issues easy to bulk-close or filter. Document that `gh issue list --label routine-reflect --state open` is the routine's issue backlog.
4. **Issue body references digest file** — if user closes an issue, the digest file still has the full context; no information is lost.

**Residual risk:** If a recurring systemic issue (e.g., watchdog stalls every week) generates a new issue every 30+ days, it will appear multiple times. Acceptable — that IS the signal the user should act on.

### 8.4 Cross-repo bleed — D2 editing outside juggle
**Risk:** RF-6 (auto-memory scan) reads files in `~/.claude/projects/.../memory/` — outside the juggle repo. The spec could allow the routine to write there, editing memory files the user didn't authorize.

**Hard boundary:** Routine 2 (reflect) has **read-only access to `~/.claude/projects/.../memory/`**. It reads, analyzes, and emits SUGGESTIONS in the digest and/or a GitHub issue. It never writes, deletes, or modifies files outside `~/github/juggle/`. This restriction is enforced by not granting the Routine write permissions to paths outside `~/github/juggle/`. The suggestion-only output means the user must manually apply any memory updates — this is intentional friction for changes to personal behavioral rules.

### 8.5 Routines feature maturity — April 2026 = ~1 month old
**Risk:** Routines launched in April 2026. At time of writing (~1 month post-launch), it may have: undocumented limitations, breaking API changes, execution environment quirks, or disappear entirely.

**Mitigations:**
1. **Local fallback spec:** The same routines can be implemented as local cron scripts (`scripts/weekly-autofix.sh`, `scripts/weekly-reflect.sh`) in under a day. Design the Routine prompt as a self-contained script; the execution layer is a thin wrapper. If Routines disappears, fall back to `launchd` or cron.
2. **Dry-run mode** (§5.5) enables testing the logic locally before committing to cloud execution.
3. **No hard dependency on Routines-specific features** — the spec uses standard `claude -p` headless mode, `git`, and `gh` CLI. These work in any execution environment.

**Fallback trigger:** If Routines is unavailable or unreliable after 4 weeks of use, migrate to local launchd plist (macOS) with identical cron expressions. Implementation time: ~30 minutes.

### 8.6 Dogfood cost ramp — $14/year for a single analysis section
**Risk:** `/schedule:dogfood` uses Sonnet at ~$0.20–0.35/run. At 52 runs/year: ~$14/year for a single routine.

**Analysis:** $14/year is ~$1.20/month. The decision to make dogfood its own routine (rather than folding it into reflect as RF-7) was made to reflect its distinct output mechanism and cost profile. That decision is locked. The question is: is Sonnet justified, or would Haiku suffice?

For qualitative cross-thread pattern analysis requiring reasoning across diverse thread summaries and code-level suggestions with file:line refs, Sonnet is justified — Haiku produces shallow, non-specific suggestions on multi-document reasoning tasks. However, this should be validated empirically.

**Options:**
1. **Keep Sonnet** — accept ~$1.20/month as the price of high-quality meta-analysis.
2. **Degrade to Haiku after 4-week trial** — if Haiku's findings are actionable, switch permanently. Saves ~$11/year.
3. **Run dogfood bi-weekly instead of weekly** — cuts cost to ~$7/year. Loses weekly recency signal.

**Recommendation:** Keep Sonnet for the first 4 weeks. Compare output quality against Haiku in week 5. If Haiku produces findings with file:line refs and concrete improvement suggestions, switch. The $1.20/month cost does not justify optimizing prematurely — validate first.

### 8.7 Schedule collision with manual work
**Risk:** Autofix runs Sunday 03:00. If the user is actively working at 3 AM (travel, different timezone, insomnia), the routine could: conflict with a `git push` the user is doing, create a branch on a dirty working tree, or cause confusion about what `cyc_schedule-autofix-*` branches exist.

**Mitigations:**
1. **Routines run in cloud** — they operate on the GitHub remote, not the local working tree. No conflict with local git state.
2. **Pre-flight check:** Routine checks `gh pr list --head "cyc_schedule-autofix-"` before starting. If any autofix branch or PR exists (from a mid-week manual run or a leftover), it skips and files an action item.
3. **Sunday 03:00 timing** — US Central time. Unlikely collision window for a single developer. If timezone changes, update the Routine trigger.

---

### 8.8 Recursive failure — dogfood analyzes a broken week
**Risk:** If Juggle's own infrastructure was broken during the analysis window (e.g., watchdog was down, DB was corrupted, most threads failed), the dogfood researcher may misdiagnose the failure as a design problem when it was an operational incident. It might suggest "fix the researcher prompt" when the real issue was a network outage.

**Mitigations:**
1. **Watchdog telemetry context:** The task prompt includes a check: "If >50% of threads this week ended in `failed` or `watchdog_retried=1`, note this as a possible infrastructure incident, not a design problem."
2. **Reflect cross-validation:** The Monday reflect digest (RF-1) runs an independent watchdog analysis. If reflect and dogfood produce contradictory diagnoses, both are visible to the user — they can reconcile.
3. **Explicit caveat in report:** The dogfood report template includes: "Note: analysis based on N threads this week. If N < 5, findings may not be representative."

**Residual risk:** The dogfood agent cannot distinguish between "the researcher prompt is bad" and "researchers were stalling because the LLM API was degraded that day." Human judgment is required for root-cause attribution. The report surfaces signals; it does not decide causes.

---

### 8.9 Action item fatigue — three weekly scheduled items
**Risk:** With three routines each capable of filing action items, the user could face: a dogfood action item (Saturday), an autofix PR to review (Sunday), and a reflect digest to read (Monday) — every week without fail. Over time, scheduled items are treated as noise and dismissed unread.

**Why separate anyway (locked decision):** Dogfood produces a `decision`-type action item because its suggestions require user judgment before dispatch. Autofix produces a PR, not an action item — it's a distinct UI surface. Reflect produces issues. These are three different surfaces, not three piles in the same queue.

**Mitigations:**
1. **Suppress dogfood action item after 3 empty weeks** (§5.8): if no findings three weeks running, stop filing. Note in reflect digest instead.
2. **Autofix PR is not an action item** — it appears in GitHub, not the Juggle action item queue. Different friction level.
3. **Reflect issues are capped at 5/week** with 30-day dedup. Most weeks: 0–2 new issues.
4. **User can disable any routine** via `claude routine disable juggle-dogfood` (or equivalent CLI). The spec does not force perpetual operation.

---

### 8.10 Confirmation bias loop — dogfood reading its own history
**Risk:** If the dogfood researcher has access to prior dogfood reports, it may pattern-match to past suggestions rather than independently analyzing current data. Week 3 report echoes week 2 report. Signal degrades into noise.

**Mitigation (enforced in prompt):**
- Task prompt explicitly states: **"Do not reference any prior dogfood reports or prior suggestions. Analyze only the raw thread data from juggle.db for the past 7 days."**
- The Routine does NOT inject prior dogfood reports into the agent's context. Context is: task prompt + DB query results. No historical report files included.
- **Rolling 4-week window** for DB query (not for report context) — ensures the agent sees recent patterns but doesn't over-index on a single bad week.

**Validation:** After 4 weeks, compare successive reports for suggestion overlap. If >70% of suggestions repeat, the prompt needs a stronger "generate fresh analysis" instruction or the analysis window needs adjustment.

---

## 9. Acceptance Criteria

The routines are considered working when all of the following are true after the **first two full weeks** of operation:

### Autofix (Routine 1)
- [ ] A PR `cyc_schedule-autofix-YYYY-MM-DD` appears in the juggle repo each Sunday by 04:00
- [ ] PR contains at least 3 of the 7 fix sections (not all will have findings every week)
- [ ] PR description includes cross-link to most recent reflect digest
- [ ] At least one GitHub issue filed with `autofix:` prefix in title
- [ ] `pytest src/ tests/` passes on the PR branch (no unreverted smoke failures)
- [ ] Total PR LLM cost < $2.00 (visible in Routines dashboard or cost tracking)

### Reflect (Routine 2)
- [ ] A file `reports/reflect-YYYY-MM-DD.md` appears in the juggle repo each Monday by 04:00
- [ ] Digest contains all 8 RF sections (partial sections marked, not absent)
- [ ] RF-7 (dogfood dispatch) embeds a non-empty research result
- [ ] At most 5 new GitHub issues per week with `routine-reflect` label
- [ ] No edits to files outside `~/github/juggle/`

### Dogfood (Routine 3)
- [ ] A file `reports/dogfood-YYYY-MM-DD.md` appears in the juggle repo each Saturday by 04:00 (after Saturday 03:00 run + up to 60 min runtime)
- [ ] A Juggle action item with `type=decision, priority=high` is filed after each successful run
- [ ] Report contains at least the "Observed Friction Patterns" section (even if empty)
- [ ] Successive reports do not >70% overlap in suggestions (checked at week 5)
- [ ] Empty-findings suppression triggers correctly after simulated 3-week quiet period

### Shared
- [ ] All three routines survive a week where there are 0 commits (no findings = no crash)
- [ ] All three dry-run modes produce output to `/tmp/` without any Git/GitHub side effects
- [ ] Dead-routine canary fires correctly when tested by manually deleting this week's PR/digest/dogfood report
- [ ] Cross-routine handoffs verified: dogfood report appears in autofix PR body; autofix PR cross-linked in reflect digest

---

## Appendix: Cost & Runtime Summary

| Routine | Step | Tool | Runtime | Cost/run |
|---------|------|------|---------|----------|
| autofix | FX-1 ruff | ruff | <1s | Free |
| autofix | FX-2 vulture | vulture | <2s | Free |
| autofix | FX-3 test gaps | pytest + claude -p | ~90s | ~$0.05–0.10 |
| autofix | FX-4 watchdog tests | sqlite3 + claude -p | ~40s | ~$0.03–0.05 |
| autofix | FX-5 doc drift | claude -p | ~60s | ~$0.03–0.08 |
| autofix | FX-6 CHANGELOG | git log + claude -p | <30s | ~$0.01 |
| autofix | FX-7 graphify | graphify CLI | ~30s | Free |
| autofix | git/gh operations | git + gh | ~60s | Free |
| **autofix total** | | | **~7–9 min** | **~$0.12–0.24** |
| reflect | RF-1 watchdog | sqlite3 + claude -p | <1 min | ~$0.02 |
| reflect | RF-2 action items | sqlite3 + claude -p | <1 min | ~$0.01 |
| reflect | RF-3 quality scoring | sqlite3 + claude -p | <1 min | ~$0.02 |
| reflect | RF-4 token outliers | sqlite3 | <5s | Free |
| reflect | RF-5 Hindsight lint | Hindsight API + claude -p | ~60s | ~$0.03–0.05 |
| reflect | RF-6 auto-memory | claude -p | ~20s | ~$0.01 |
| reflect | RF-7 skill drift | sqlite3 + claude -p | ~50s | ~$0.02 |
| reflect | RF-8 dogfood pulse | file read (no LLM) | <5s | Free |
| reflect | gh issue creation | gh CLI | <30s | Free |
| **reflect total** | | | **~6–8 min** | **~$0.11–0.11** |
| dogfood | research agent (Sonnet) | Juggle agent / claude -p | ~5–8 min | ~$0.20–0.35 |
| dogfood | commit + action item | git + juggle_cli.py | ~30s | Free |
| **dogfood total** | | | **~6–9 min** | **~$0.20–0.35** |
| **combined weekly** | | | **~19–26 min** | **~$0.43–0.70** |
| **combined annual** | | | | **~$22–36/year** |
