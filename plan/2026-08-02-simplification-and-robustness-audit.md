# Simplification & Robustness Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find out which parts of juggle are actually used, which are actually fragile, and retire or harden them on evidence rather than intuition — without paying for the instrumentation with more complexity than it removes.

**Architecture:** Three sources of truth, in order of cost. (1) **Retroactive mining** — Claude Code transcripts, git forensics, and the four DB ledgers juggle already writes; no new code, answers most questions immediately. (2) **One forward instrument** — a `feature_usage` counter cloned from the *already-working* `agent_tool_events` UPSERT-aggregate pattern, wired at exactly two choke points every feature already flows through (the CLI verb dispatcher and the hook dispatcher). (3) **A scorecard** joining usage × failure × LOC × churn, whose rows resolve to keep / harden / fold / delete.

**Tech Stack:** Python 3.12+ (stdlib only), SQLite, `git log`, `grep` over `~/.claude/projects/**/*.jsonl`.

---

## Design decision — why NOT tracing per feature

The obvious move is "build tracing into the major features." Rejected, for three reasons:

1. **It is the disease, not the cure.** Instrumenting ~60–80 features across 56,637 LOC means touching every module the audit is meant to shrink, and every touch is a new line to maintain, test, and eventually delete. A simplification project whose first act is to add code to every feature has already lost.
2. **The failure half is already instrumented.** `error_events`, `selfheal_audit`, `watchdog_events`, and `agent_runs` already record what breaks. Nothing needs adding there — what's missing is a *denominator* (uses), not more numerators.
3. **The usage half is recoverable retroactively.** Every `juggle_cli.py` invocation juggle has ever made is sitting in the Claude Code transcripts. It was measured (see Fact 6) and it works. Waiting a month for fresh telemetry when 12 days of history is already on disk is a self-inflicted delay.

So: mine first, instrument once, at two seams, with a pattern that already exists in this repo.

**Cost asymmetry that drives the sequencing:** transcripts age out (Fact 6 — oldest surviving file is 12 days old), and the prod DB was reset once already (Fact 3). Retroactive sources are *perishable*. Phase 1 therefore runs before anything else, including before the counter is built.

---

## Measured facts this plan relies on (verified 2026-08-02 on `main` @ `c5a0b16`)

### Fact 1 — the "thin orchestrator" is no longer thin

| Metric | Value |
|---|---|
| `src/**/*.py` LOC | **56,637** across **355** files |
| Files over the repo's own 300-line gate | **25** (grandfathered allowlist) |
| Migrations | 33 |
| `commands/*.md` | 31 |
| Test files | 448 |

### Fact 2 — half the project's energy is interest payments

Last 8 weeks: **1,014** non-merge commits — **289 `fix:`** vs **256 `feat:`**, a fix:feat ratio of **1.13** (it was 1.05 when first sampled on 2026-07-26 — the ratio is getting *worse*, not better).

Highest-churn source files (touches in 8 weeks): `scripts/loc_gate.py` 65, `juggle_graph_dispatch.py` 46, `juggle_cockpit.py` 46, `juggle_cmd_integrate.py` 37, `juggle_watchdog_daemon.py` 32, `juggle_settings.py` 32, `juggle_cmd_graph.py` 32, `juggle_cockpit_modals.py` 28.

> `loc_gate.py` topping the churn list by a wide margin is itself a finding: the allowlist is edited more often than any real module. Either modules are being split constantly (good) or the gate is being negotiated with (bad). Task 3.6 resolves which.

### Fact 3 — the DB is not a long-horizon record

`nodes` spans **2026-07-26 00:11 → 2026-08-01 22:07** — about 7 days. The DB was reset/rebuilt on 2026-07-26 (nodes migration or `consolidate_dbs.py`). **Consequence: any forward usage telemetry must be durable across DB resets** (Task 2.4 mirrors to append-only JSONL).

### Fact 4 — the self-heal loop catalogues errors but never closes them

> **ROOT-CAUSED 2026-08-02 during Phase 1 — see Execution Log R3. The diagnosis below ("the classifier is not classifying") was WRONG. The real cause is a false-positive attribution bug: `_attribute_tool_errors` blames juggle for every failed tool call that merely happens in the same turn as a juggle command. All 23 rows are false positives. Phase 4.1 is rewritten accordingly.**

| Ledger | 2026-07-26 | 2026-08-02 | Note |
|---|---|---|---|
| `error_events` (status=`open`) | 19 | **23** | **zero** have ever left `open` |
| `selfheal_audit` | 12 | **15** | **every** row's action is `new_variant` |

Worse: all 23 `error_events` rows have `error_class = 'B'` and `exc_type = NULL`. The classifier is not classifying — it is a single-bucket counter with an unused schema. This is the single highest-value robustness target in the repo.

`watchdog_events` (5 total): `recovered` 3, `topic_unmerged_orphan` 1, `retry_blocked` 1.
`agent_completions`: coder 15 @ 354 s avg, researcher 4 @ 496 s avg.

### Fact 5 — zero-usage surfaces are already visible

`loops` **0 rows**, `project_corrections` **0 rows**, `notifications` (v1) **0 rows** while `notifications_v2` holds 78. Three tables carrying schema, migrations, and code paths for traffic that isn't happening.

> **Caveat that must be honoured before deleting any of these:** the DB is only 7 days old (Fact 3). Zero rows in a 7-day window is *suggestive*, not conclusive — `loops` in particular is a shipped feature (Loop V2, TODO.md) that may simply be unused *by this operator*. Deletion requires the transcript check in Task 3.3, not just the row count.

### Fact 6 — transcripts are a working retroactive tracer, and they are perishable

74 `*.jsonl` files across 9 project dirs; **oldest surviving file is dated 2026-07-21** — a ~12-day retention window. The histogram works today:

```bash
grep -rhoP 'juggle_cli\.py\s+\K[a-z-]+( [a-z-]+)?' ~/.claude/projects/ | sort | uniq -c | sort -rn
```

```
219 agent complete    32 agent get       16 action ack      12 thread create    11 action list
 62 agent fail        27 agent list      15 verify          12 action notify     7 thread messages
 35 recall            23 agent send-task 11 thread list      7 graph show        7 graph mark-task
```

> **SUPERSEDED 2026-08-02 — these raw-grep counts are inflated.** The grep counts every textual occurrence, including tool *results*, echoed output, and text inside agent prompts. Parsing the transcripts properly (only `Bash` `tool_use` blocks) gives **299 real invocations across 48 distinct verbs**, not ~500. `agent complete` is **43**, not 219; `recall` is **8**, not 35. The corrected table is in Execution Log R2. **Lesson for the rest of this plan: count tool_use blocks, never raw text.**

Two findings fall straight out of that one command:

- **`recall` is invoked and has no parser entry.** `docs/ARCHITECTURE.md` lists `recall` under "Removed commands (no replacement)", and `grep` over `juggle_cli_parsers_*.py` finds nothing. So either something is still calling a dead verb, or the doc is wrong. Either way it is a real defect surfaced by one grep. → Task 1.5. **CONFIRMED as a live defect — see Execution Log R1.**
- **The agent lifecycle dominates.** `agent complete`/`get`/`list`/`send-task` are the top verbs by a wide margin. The agent lifecycle *is* the product. Anything in the top 5 is a "harden, never touch casually" row before the scorecard is even built.

### Fact 7 — the counter this plan needs already exists and works

`agent_tool_events` was initially mis-read as a dead instrument (4 rows, unchanged for a week). It is not dead — it is an **UPSERT aggregate**:

```
['id','role','tool_name','mode','count','first_seen','last_seen','last_input']
(1, 'coder',      'Edit',  'normal', 76, '2026-07-26T00:24:50Z', '2026-08-01T22:18:15Z', 'file_path=…')
(18,'coder',      'Write', 'normal', 21, '2026-07-26T03:55:03Z', '2026-08-01T22:15:48Z', 'file_path=…')
```

4 rows, counts of 76 and 21, `last_seen` current. Writer at `src/dbops/agents.py:235`.

**This changes Phase 2 from "design a telemetry system" to "clone a proven in-repo pattern at two more choke points."** Same shape (`count` + `first_seen`/`last_seen`, one row per distinct key), same module, same test conventions.

### Fact 8 — instructions have drifted from reality

- `CLAUDE.md` mandates reading `graphify-out/GRAPH_REPORT.md` and prefers `graphify query` over grep for multi-file searches. **`graphify-out/` does not exist.** Every session pays for a directive that cannot be followed.
- `docs/ARCHITECTURE.md` documents the `threads`/`notifications` schema; the live DB is `nodes`/`node_edges`/`notifications_v2`. The doc describes a schema the code no longer has.

Stale instructions are complexity with a 100% hit rate — every agent, every session, forever. They are in scope for this audit (Task 5.4).

---

---

## Execution Log — Phase 1 results (2026-08-02)

Phases 0–1 were executed the same day the plan landed. Findings below; they amend the plan above where marked.

### R0 — transcripts snapshotted (Task 1.1 ✅)

75 files / 40 MB copied to `~/.juggle/transcript-archive/2026-08-02/`, owner-only perms, `MANIFEST.txt` written. Kept **outside the repo deliberately** — transcripts contain credentials and user data and must never enter a pushed git repo. Retention window confirmed: oldest surviving file 2026-07-21, i.e. **12 days**.

### R1 — Task 1.5 `recall` phantom: CONFIRMED LIVE DEFECT ✅ (fix dispatched)

8 real invocations. Every direct one exits 2 with an argparse usage error; several were piped through `2>&1 | head -100`, which **masked the non-zero exit**, so the failure was silent. Four more were embedded into dispatched *agent task prompts*, so tmux agents inherited the broken instruction.

Root cause: `commands/start.md` — the main orchestration prompt — mandates `juggle_cli.py recall` in **three places** (lines 136, 194, 336), including the hard rule *"Never answer from training data alone"*. `commands/resume-topic.md:26` mandates `recall-bg`. Both verbs were deleted in the resource-verb grammar migration (`ARCHITECTURE.md:356-358`, "no replacement"). Meanwhile `juggle_hindsight.py:174` still has a **working** `recall()` already used by `juggle_cmd_research.py:119`.

So the personal-question recall rule has been silently no-op'ing since the removal. This is CLAUDE.md's "code over prompts" principle failing in reverse: the code was deleted and the prompt kept mandating it.

**Decision (staff-level):** restore as `memory recall <id> <query>` under the resource-verb grammar, next to the existing `memory retain` — this honours the migration's intent (the verb now *has* a resource home) rather than reverting it. Plus a **drift guard** test that parses every `commands/*.md` for `juggle_cli.py <verb>` and asserts each resolves in the parser, which kills the whole bug class.

### R2 — corrected usage histogram (Task 1.2 ✅)

**299 invocations, 48 distinct verbs** (tool_use blocks only — see the correction on Fact 6). Top: `agent complete` 43, `agent get` 30, `agent list` 28, `agent send-task` 21, `action ack` 16, `thread list` 12, `thread create` 12, `action list` 11, `graph show` 9, `thread messages` 9.

**Attribution caveat:** the `user`/`agent` split came out 299/0, which is an artefact — juggle's tmux agents run as their own top-level Claude Code sessions, not as `isSidechain` subagents, so they are indistinguishable from operator turns by that signal alone. Use `cwd`/`gitBranch` (worktree paths, `cyc_*` branches) to split them, or rely on Phase 2's `source` column, which does not have this problem.

### R3 — Task 4.1 root-caused: the error ledger is ~100% false positives 🔴 (fix dispatched)

Inspecting all 23 rows: **not one is a juggle defect.** They are ordinary shell friction from agent work — `ssh: Host key verification failed`, `Could not resolve hostname services`, `ansible-playbook is not provided by package 'ansible'`, `tofu not found`, a `git rebase` CONFLICT in another repo, `grep` exiting 1 on no match, `command not found: sqlite3`, a Python heredoc `SyntaxError`, and a `Read` of a nonexistent file. Several rows are **this audit's own commands**.

Root cause: `_attribute_tool_errors` (`src/juggle_hooks_classb.py:141`) uses an **N=10 same-turn proximity heuristic**. It concatenates the last 10 tool inputs, and if *any* string in `_JUGGLE_PATHS` appears anywhere in that blob, it attributes **every** errored tool_result in the turn to juggle. `_JUGGLE_PATHS` includes `"commands/"` and `"juggle:"` — so merely running a juggle command marks the whole turn. Textbook *post hoc ergo propter hoc*.

This explains every symptom in Fact 4 at once: `exc_type` is NULL because there is no exception (it is tool-result text); `surface` says `juggle_cli.py` for an `ssh` failure because that is the matched `juggle_ref`, not the failing command; nothing is ever resolved because there is nothing to fix; and every distinct shell failure is a new signature forever, which is why all 15 `selfheal_audit` rows say `new_variant`. `juggle_metrics_errors.py:49` reports this count as "Juggle-caused", so **any KPI built on it is meaningless**.

**Fix dispatched:** attribution must be causal — an errored tool_result may only be recorded if *its own* originating tool_use references a juggle path (`tc` is already resolved in the loop at line 177). Regression pin required. The 23 existing rows are NOT bulk-deleted pending a recommendation.

**Amends Phase 4.1:** the task is no longer "make the classifier classify." It is "stop the detector from inventing defects." The open-error KPI only becomes meaningful after this lands.

### R4 — Open Question 1 answered: no data loss, no defect ✅

The 2026-07-26 "reset" was a **fresh DB creation**, not lost history. The three `juggle.db.pre-migrate.*` snapshots from Jul 25 23:59 / Jul 26 00:05 / Jul 26 00:11 all contain **nodes=0, messages=0** — 319 KB schema-only files from consecutive `doctor`/migration runs. The 2026-08-01 backups (`bak-pre-1.128.1`, `pre-migrate.1785621899`) hold 37 nodes / 217 messages, consistent with normal growth. The 7-day span is simply the DB's real age. **OQ 1 closed — no defect, and it does not block anything.**

### R5 — the scorecard's negative signal is NOT trustworthy without Phase 2 ⚠️

Walking the argparse tree gives **100 leaf verbs**; only **29** were invoked in the window, leaving 71 "never invoked."

**Do not read that as a delete list.** The transcript method sees only *shell* invocations from Claude Code sessions. It is structurally blind to cron/launchd (`schedule dogfood|autofix|reflect`), the watchdog daemon, skill-mediated calls, and internal Python callers. Cross-referencing the 71 against `commands/`, `skills/`, `src/`, `scripts/`, `hooks/`:

- **60 are referenced elsewhere** — machinery, not dead. E.g. `graph load` (33 src refs), `agent fail` (31), `graph reconcile` (17), `loop create` (14).
- **11 have no reference outside tests** — the genuine strong candidates: `agent set-watchdog`, `context digest`, `context show`, `graph add-node`, `node create`, `research run`, `selfheal set-status`, `selfheal show`, `thread list-stale`, `thread set-summarized-count`, `thread unarchive`.

**This validates the Phase 2 design decision.** Transcripts give high-confidence KEEP verdicts and only *candidate* DELETE verdicts; the counter at the CLI dispatcher is what closes the gap, because it sees every caller regardless of origin. **No verb may be deleted on transcript evidence alone.**

### R6 — the documented legacy alias shim is empty

`juggle aliases --json` returns `{}`. `ARCHITECTURE.md` states the old flat hyphenated forms "still resolve through the backward-compat alias shim" and directs readers to that command "for the full legacy→canonical map." The map has no entries — a documented compatibility promise with nothing behind it. Fold into Task 3.3 (verify, then delete the shim or the docs).

### R7 — skills coverage looks thin, with a caveat

Only 3 of juggle's 31 `commands/*.md` appear as `<command-name>` invocations in 12 days (`juggle:start`, `juggle:init`, `juggle:toggle-autopilot`); the rest of the traffic is built-ins (`clear` 24, `model` 10, `effort` 3). **Caveat before acting:** skills invoked indirectly (by the watchdog, by other skills, or by path rather than slash-command) will not appear as `<command-name>`, so this shares R5's blind spot. Treat as a Phase 2 question, not a deletion list.

---

## Phase 0 — Define the unit of analysis

The unit is a **feature**, not a file. A file can be 90% dead while its module stays busy.

- [ ] **0.1** Enumerate every feature into `data/simplify/features.csv`. Sources: `juggle aliases --json` (canonical verbs + legacy aliases), the three `juggle_cli_parsers_*.py` modules, `commands/*.md` (31), the hook handlers in `juggle_hooks*.py`, each watchdog playbook/sweep, each cockpit panel/modal/screen, the 3 schedules, talkback, research KB, vault, and each `juggle_vcs_recipes/*` backend. Expect **60–80 rows**.
- [ ] **0.2** Columns: `feature, kind, owns_loc, human_uses, agent_uses, machine_uses, errors_attributed, fix_commits_8w, test_count, verdict, notes`. `verdict ∈ {keep, harden, fold, delete, undecided}`, everything starts `undecided`.
- [ ] **0.3** Attribute LOC to features, not files. Where a module serves several features, split the count by rough responsibility rather than double-counting; note the split in `notes`.

**Definition of done:** `features.csv` exists with every row enumerated and `verdict=undecided`.

---

## Phase 1 — Mine the perishable sources FIRST (no new code)

> **Do this phase before anything else.** Transcripts are 12 days from the edge of retention (Fact 6).

- [ ] **1.1 Snapshot the transcripts.** Copy `~/.claude/projects/**/*.jsonl` to a dated archive outside the retention sweeper *before* analysis. Losing this data costs weeks of waiting.
- [ ] **1.2 Build the CLI usage histogram** with the Fact 6 grep. Widen the pattern to catch `uv run src/juggle_cli.py`, `python3 …`, and the plugin-path form. Fill `features.csv:human_uses` / `agent_uses`, attributing by whether the call sits in a user turn or an agent/hook turn.
- [ ] **1.3 Build the skill/command histogram** from `<command-name>` markers in the same transcripts → coverage for all 31 `commands/*.md`.
- [ ] **1.4 Record the retention window** (oldest transcript date) in the scorecard header. Every "zero uses" claim is only as strong as that window, and readers must see it.
- [ ] **1.5 Resolve the `recall` phantom (Fact 6).** 35 invocations, no parser entry. Determine whether calls are historical (pre-removal) or live-and-failing. If live: fix the caller or restore the verb; if historical: nothing to fix, but note the transcript window straddles the removal. **Do not skip — this is a live-defect candidate under the autopilot DEFECT PROTOCOL.**
- [ ] **1.6 Git forensics → fragility index.** Per module: `fix:` commits touching it ÷ total touches, last 8 weeks. Fill `fix_commits_8w`. The LOC-gate allowlist (`scripts/loc_gate.py`) is a ready-made burn-down list — every entry is a module that already failed the architecture gate.
- [ ] **1.7 Harvest prior dogfood reports** (2026-07-11 → 2026-08-01). They already name systemic root causes: label-collision whack-a-mole (three callsites, no canonical resolver), the 5-fix agent-lifecycle cascade, atomicity discovered post-landing, the watchdog-sweep pre-wiring gap, unguarded graph-mutation node kinds. These are findings already paid for — fold them into the scorecard rather than rediscovering them.
- [ ] **1.8 Fill the failure columns** from `error_events`, `selfheal_audit`, `watchdog_events`, `agent_runs` (queries in Fact 4).

**Definition of done:** every `features.csv` row has usage and failure numbers, or an explicit `no-data` marker.

---

## Phase 2 — Build the ONE missing instrument

Clone `agent_tool_events` (Fact 7). Do not invent a new shape.

- [ ] **2.1 Reserve the migration number with `juggle migration next`.** Never hand-pick (CLAUDE.md; 2026-07-02 duplicate-column incident).
- [ ] **2.2 Add `feature_usage(feature, source, count, first_seen, last_seen)`**, UPSERT on `(feature, source)`, modelled on `src/dbops/agents.py:235`.
- [ ] **2.3 Wire exactly two choke points:** the `juggle_cli.py` dispatcher immediately after argparse resolves `resource.verb`, and the `juggle_hooks.py` dispatcher. **No per-feature instrumentation.** If a third seam seems necessary, that is a signal the feature bypasses the CLI/hook contract — record it as a finding instead of adding a seam.
- [ ] **2.4 `source ∈ {user, agent, watchdog, schedule, cockpit}`** — non-negotiable. Watchdog ticks and cockpit refreshes will otherwise dominate the histogram and make machinery look beloved. The query that matters is *features with zero `user`-sourced hits that also have no machine dependency*.
- [ ] **2.5 Mirror every increment to append-only JSONL** under `CLAUDE_PLUGIN_DATA` (or `~/.juggle/`), so the record survives the next DB reset (Fact 3).
- [ ] **2.6 Fail-quiet, and only here.** The telemetry write is wrapped in `try/except` — a counter must never break a command. This is the one place in juggle where fail-quiet is correct; note the exception explicitly in the module docstring so it doesn't get "fixed" into fail-loud by a later reviewer.
- [ ] **2.7 Add a liveness check.** Something must assert the counter is still recording — a `doctor` check that flags `max(last_seen) > 48 h` as stale. An instrument that silently stops is worse than none: it produces confident wrong answers.
- [ ] **2.8 Bake 2–4 weeks before any deletion decision relies on forward data.** Phases 3–4 can proceed on Phase 1 evidence meanwhile.

**Definition of done:** full suite green; `feature_usage` rows appear for both a CLI call and a hook fire, with distinct `source` values; `doctor` reports counter liveness.

---

## Phase 3 — Decide: usage × failure-per-use

Now `error_events ÷ feature_usage` finally has a denominator.

| | **Low failure** | **High failure** |
|---|---|---|
| **High use** | **Keep. Do not touch.** | **Harden** — the real robustness backlog |
| **Low/zero use** | **Delete** | **Delete first** (paying to maintain bugs nobody hits) |

- [ ] **3.1** Assign every `features.csv` row a verdict. No row stays `undecided`.
- [ ] **3.2 Protect the keepers.** The top-5 by usage (Fact 6: `agent complete`, `agent fail`, `agent get`, `agent list`, `agent send-task`) are load-bearing. The biggest risk of a simplification campaign is destabilising what works.
- [ ] **3.3 Delete candidates, each requiring a transcript check first** (not just a row count — Fact 5's caveat): `loops` (0 rows — but Loop V2 is shipped; check whether it's unused or merely unused *here*), `project_corrections` (0), `notifications` v1 (0 vs v2's 78), the legacy alias shim (grep transcripts for legacy hyphenated forms before removing), unexercised `juggle_vcs_recipes/*` backends, unused cockpit viewport profiles.
- [ ] **3.4 Prefer concept mergers over file deletions** — they remove *understanding* cost, which is the expensive kind:
  - **Three "tell the user something" channels** — `notifications_v2`, `action_items`, cockpit notify → one outbox.
  - **Two label-resolution paths** → the canonical `resolve_label()` the dogfood reports have specified twice (spool + cockpit callsites).
  - **The big one: two agent systems.** The tmux pool (`juggle_tmux.py` 878 LOC + pool/lifecycle/worktree modules, ~2k LOC) vs Claude Code background `Agent`/Task. The scorecard must answer which path real work flows through and whether the other can die. **This single question is worth more than the rest of Phase 3 combined** — treat it as its own spike with its own written verdict, and do not let it be answered casually.
- [ ] **3.5 Docs are features too.** `docs/ARCHITECTURE.md`'s `threads` schema and `CLAUDE.md`'s graphify directive (Fact 8) get verdicts alongside code.
- [ ] **3.6 Resolve the `loc_gate.py` churn signal** (Fact 2): are modules genuinely being split, or is the allowlist being negotiated with? If the latter, the gate is theatre and needs teeth.
- [ ] **3.7 Retirement protocol.** Tag `pre-simplify-2026-08` first. One commit per removed feature (each independently revertible). Regression pins retargeted through new seams, never weakened — **and the retirement list goes to the user for approval before any pin is touched** (CLAUDE.md regression-pin gate). `ARCHITECTURE.md`/`CLAUDE.md` updated in the same commit as the removal. Table drops land in a *later* migration, after a bake period — code first, schema second.

**Definition of done:** every row has a verdict; the deletion list has explicit user approval where pins are affected.

---

## Phase 4 — Harden the keepers, ranked by failure-per-use

- [x] **4.1 Fix the error ATTRIBUTION first — rewritten after R3.** ~~Make the classifier classify.~~ The classifier was never the problem: `_attribute_tool_errors` blames juggle for unrelated shell failures that merely share a turn with a juggle command, so all 23 rows are false positives. Fix = causal attribution (per-tool_result, not per-turn) + regression pin. **Dispatched 2026-08-02.**
- [ ] **4.1b Only after 4.1 lands:** add `resolved`/`benign` transitions and make open-error count a weekly KPI driven toward zero. Doing this before 4.1 would just be triaging noise.
- [ ] **4.1c Decide what to do with the 23 existing false-positive rows.** Deliberately left in place — bulk-deleting rows is the kind of irreversible cleanup that needs a decision, not a default.
- [ ] **4.2 Give self-heal teeth.** All 15 audit rows are `new_variant` — it recognises novelty and stops. A signature seen ≥N times should auto-file a pinned repro task. **Note: expect this to look very different once 4.1 lands** — with false positives gone, the real signature volume may be near zero, and "self-heal has nothing to do" is a legitimate outcome worth measuring before building more machinery on top of it.
- [ ] **4.3 Adopt the scope-before-fix rule.** The Bug#1 lesson (four live-proof iterations because scope was guessed): before fixing a bug *class*, write the enumeration test naming every affected kind. Codify in `CLAUDE.md`.
- [ ] **4.4 Prefer deleting a state over adding a guard.** The 2026-07-19 cascade (5 lifecycle fixes in a day) and the recurring `nodes.state` fixes say the state machine has too many reachable states. An explicit transition table asserted at one `dbops` seam converts whack-a-mole into a single gate. Live states today: `verified, open, archived, background, done, failed-exec, failed-integration, integrating, integrated-unlanded, delivered, cancelled` — **11+ states is itself the finding.**
- [ ] **4.5 Ship the two standing dogfood recommendations:** canonical `resolve_label()`, and the module-wiring smoke test (feat→fix-wiring within minutes is a definition-of-done gap, not bad luck).
- [ ] **4.6 Enforce-or-delete every "documented only" limit** in `docs/ARCHITECTURE.md` ("Max background agents 3 — No (documented only)", "Agent timeout 15 minutes — No"). A limit that isn't code is a lie the system tells its operator.
- [ ] **4.7 Adopt the watchdog-sweep pre-wiring gate** from dogfood 2026-08-01: a new sweep needs zero-candidates, all-junk, apply-failure, and import-path tests **before** it is wired into the tick.

---

## Phase 5 — Institutionalise, so this never needs a second campaign

- [ ] **5.1 Fix the dogfood DB snapshot — 4th consecutive week unapplied** (`src/schedules/dogfood.py`, `TASK_PROMPT_TEMPLATE` + `run()`; ~25–30 lines, fully specified in the 2026-07-18 report). The self-analysis flywheel has been running blind for a month. **Highest ratio of value to effort in this entire plan.**
- [ ] **5.2 Extend the weekly dogfood report** with: bottom-5 features by `user` usage, top-5 by failure-per-use, LOC + allowlist delta, open-error trend.
- [ ] **5.3 Monthly ritual:** the bottom-3 usage rows each get an explicit keep/fold/delete decision recorded in `TODO.md`. Continuous pruning beats a second campaign.
- [ ] **5.4 Reconcile instructions with reality (Fact 8):** either generate `graphify-out/` or drop the directive from `CLAUDE.md`; correct `ARCHITECTURE.md`'s schema section to `nodes`/`node_edges`/`notifications_v2`.

---

## Success metrics (all currently measured — 2026-08-02 baseline)

| Metric | Baseline | Direction |
|---|---|---|
| `src` LOC | 56,637 | ↓ |
| Files over 300-line gate | 25 | ↓ |
| Open `error_events` | 23 (0 ever closed) | ↓ toward 0 |
| fix:feat ratio (8w) | 1.13 | ↓ below 1.0 |
| Features with `verdict=undecided` | 100% | → 0% |

**Capability must stay constant.** A drop in LOC bought with a drop in what juggle can do is not a win.

---

## Open questions (resolve at staff level during execution; do not block on the user)

1. **Why did the DB reset on 2026-07-26?** (Fact 3.) If it was `consolidate_dbs.py` or a nodes migration, fine. If it was unplanned data loss, that is a defect outranking this whole plan under the autopilot DEFECT PROTOCOL.
2. **Is `loops` genuinely unused, or unused only by this operator?** Loop V2 shipped (TODO.md, 2026-07-05). Deleting a working feature because one operator hasn't used it in 7 days would be a serious error — Task 3.3's transcript check is the guard.
3. **Which agent system wins** (Task 3.4)? This is the largest single simplification available and the one most likely to be a genuine product-direction fork. If the evidence is genuinely balanced, escalate; if not, decide and record why.

---

## First moves, in order

1. [x] **Snapshot transcripts** (1.1) — perishable, 12 days from the edge. ✅ R0
2. [x] **Usage histogram** (1.2) — highest information gain per unit effort in the plan. ✅ R2
3. [x] **Triage the 23 open error events** (4.1) + answer why the DB reset (OQ 1). ✅ R3 (root-caused, fix dispatched) / R4 (no defect)
4. [ ] **Ship the dogfood DB fix** (5.1) — 4 weeks overdue, ~30 lines. **Dispatched 2026-08-02** (needs an extract-first refactor: `dogfood.py` is 401 lines against a 407 allowlist pin, so a 30-line addition blows the LOC gate).
5. [x] **Build the scorecard** from Phases 0–1 and mark first verdicts. ✅ R5 — 100 leaf verbs, 29 used, 11 strong delete candidates. **Negative signal not actionable until Phase 2 lands.**

### Next, once the three dispatched fixes land

6. [ ] Re-run R2/R5 against the post-fix tree and diff the scorecard.
7. [ ] Build the Phase 2 counter (2.1–2.7) — it is now the gating dependency for **every** delete verdict (R5).
8. [ ] Work the 11 strong delete candidates (R5) — each still needs the Phase 2 confirmation before removal.
9. [ ] Settle the two-agent-systems question (3.4), the largest single simplification available.

## Working rules adopted during execution

- **Always `git pull` before starting a fix and again before landing** — including in every agent brief. Adopted 2026-08-02 after three concurrently-dispatched agents went 4 commits stale mid-flight. A full-suite green on a stale base proves nothing about the merged result.
- **Count `tool_use` blocks, never raw text**, when mining transcripts (Fact 6 correction).
- **Never delete on transcript evidence alone** (R5).
