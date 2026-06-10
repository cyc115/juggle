# Refactor for Understanding & Token Efficiency — Phased Plan

Date: 2026-06-10
Goal (Stage 1): small single-purpose modules (≤300 lines per repo CLAUDE.md architecture gate),
no duplicated logic, domain-grouped namespaces, findable tests, and a navigation layer —
all behavior-preserving, full suite green at every commit.

---

## Baseline metrics (measured 2026-06-10, HEAD = 88dc30e)

| Metric | Value |
|---|---|
| Total Python LOC (git-tracked `*.py`) | **43,664** across 143 files |
| scripts/ (no `.py` ext, not counted above) | 1,544 LOC across 7 scripts |
| `src/` modules >300 lines | **22** |
| All `.py` files >300 lines (src + tests) | **50** |
| Test files | 100 (`tests/`, `tests/schedule/`, `tests/watchdog/`) |
| Tests collected | **1390** (`pytest --collect-only -q`: `1390 tests collected in 0.51s`) |
| Graph (graphify, commit b378fac) | 6667 nodes · 11633 edges |

Top-10 src LOC:

```
1962 src/juggle_db.py          1098 src/juggle_cmd_agents.py
1332 src/juggle_watchdog.py    1056 src/juggle_hooks.py
1120 src/juggle_cockpit.py     1006 src/juggle_cli.py
 839 src/juggle_tmux.py         823 src/juggle_schedule_autofix.py
 737 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
```

God nodes (graphify GRAPH_REPORT.md):

```
JuggleDB 1392 edges · JuggleTmuxManager 474 · Agent 228 · CockpitApp 220
HindsightClient 200 · Notification 198 · Action 196 · CockpitState 132
```

Import cycles: only a trivial self-cycle in `src/harnesses/__init__.py`. No real cycles — decomposition is low-risk on that axis.

## Survey findings (evidence, not vibes)

### F1 — src/ is NOT a package; plugin wiring pins file paths
- No `src/__init__.py`. Every entry module does `sys.path.insert(0, <src dir>)` and
  imports siblings flat (`from juggle_db import JuggleDB`).
- `hooks/hooks.json` invokes `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_hooks.py <event>`
  directly (UserPromptSubmit, Stop, SessionStart, PreCompact, PreToolUse×N) plus
  `scripts/talkback-stop-hook`.
- **26 plugin files** reference `src/juggle*` paths: `hooks/hooks.json`, 20 `commands/*.md`,
  4 `skills/*/SKILL.md`, `.claude-plugin/skills/next-action.md`.
- `src/juggle_cli.py` is a `uv run --script` PEP-723 script (inline deps: rich, httpx, pyte, pyyaml).
- Watchdog hot-restart staleness watches `src/juggle_watchdog.py` by path and
  `_collect_mtimes(src_dir)` scans the flat src dir (`scripts/juggle-agent-watchdog:40`,
  `src/juggle_watchdog.py:115`). Moving watchdog files changes what mtime collection sees.
- **Constraint:** entry-point files (`juggle_cli.py`, `juggle_hooks.py`) must keep their
  paths, or every reference must be updated atomically. Subpackaging under `src/` works
  without packaging changes because of the existing `sys.path.insert(0, src)` idiom —
  `from db.core import JuggleDB` resolves as a plain subdirectory with `__init__.py`.

### F2 — Verified duplication
1. **Pidfile/singleton daemon logic** — `scripts/juggle-agent-monitor:31-90`
   (`_is_monitor_process`, `_kill_existing_monitor_from_pidfile`, `_write_singleton_pid`)
   is a near-copy with drift of `src/juggle_watchdog.py:1039-1098`
   (`_is_watchdog_process`, `_kill_existing_watchdog_from_pidfile`) and
   `scripts/juggle-agent-watchdog:47-67` (`_write_singleton_pid`). Diff confirmed: same
   shape, divergent kill semantics (monitor kills process group + waits; watchdog sends
   single SIGTERM). The drift is itself a latent bug class.
2. **`claude -p` subprocess wrappers — 4 implementations:**
   - `src/juggle_schedule_common.py:178 claude_p()` (cost-tracked, JSON output)
   - `src/juggle_project_summary.py:10 _claude_sonnet()` (plain, 120s timeout)
   - `src/juggle_cmd_projects.py:447 and :686` (two inline `subprocess.run(["claude","-p",...])`)
   - `src/juggle_cli_common.py:153 llm_call()` (OpenRouter primary → claude fallback, profiles)
3. **Schedule-common tests duplicated across two locations:**
   `tests/test_juggle_schedule_common.py` (647 lines, class-based) and
   `tests/schedule/test_schedule_common.py` (357 lines, function-based) test the SAME
   module (`load_state`, `save_state`, `mark_run_complete`, `last_run_ts`, `gh_*`,
   `claude_p`, CostTracker...). Overlapping but not identical — consolidation must union
   the assertions, never drop any.

### NOT duplicated (verified, do not "fix")
- `src/juggle_schedule_*.py` already share `juggle_schedule_common.py` (all three import it).
- `scripts/juggle-agent-watchdog` is already a thin-ish wrapper importing 15+ functions
  from `src/juggle_watchdog.py` — its remaining 247 lines are loop + pidfile + signal
  handling (pidfile part is dup #1 above).
- `scripts/juggle-selfheal-monitor` (81 lines) has no pidfile logic and imports settings
  from src — already thin.

### F3 — Half-finished decompositions
- **Cockpit:** `juggle_cockpit.py` (1120) coexists with `_view` (499), `_model` (467),
  `_modals` (200), `_helpers` (200), `_widgets` (130). The monolith still contains:
  column-ratio persistence helpers (~100 lines), `CockpitApp` with ~30 `action_*`/event
  methods (~625 lines), `run()`, and a **profiling subsystem** (`_parse_psrecord_log`,
  `_profile_worker_loop`, `run_profile`, ~300 lines) that has no business in the app module.
- **Schedule:** `juggle_schedule_common.py` extraction done; the three schedule scripts
  remain >400 lines each but are mostly genuine routine logic.
- **Watchdog:** `juggle_watchdog_health.py` exists, but `juggle_watchdog.py` (1332) still
  mixes pure classifiers, hot-restart policy, recovery execution, orphan checks,
  `inspect_agent`, and pidfile process management.

### F4 — `juggle_db.py` seams already labeled
`JuggleDB` (84 methods, 1392 graph edges) has explicit section comments at lines
815/901/999/1042/1219/1292/1296/1428/1503/1557/1647/1675: Session helpers · Thread
operations · Thread state machine · Project operations · Message operations · Shared
context · Notifications (+v2) · Action items · Self-heal error_events · Archive ·
Agent pool. These are the mixin seams.

### F5 — Tests unfindable by topic
- Vague names: `test_cockpit_features_v2.py` (738), `test_cockpit_model_v2.py`,
  `test_context_injection_v2.py`, `test_schema_v2.py`.
- Date-stamped regression file: `test_watchdog_event_regression_2026_05_18.py` (586).
- Mega test files >700 lines: `test_juggle_cli.py` 1058, `test_juggle_tmux.py` 1033,
  `test_cockpit_keys.py` 1021, `test_juggle_db.py` 746, `test_cockpit_features_v2.py` 738,
  `test_watchdog_stall_regressions.py` 722.
- Topic subdirs already exist (`tests/schedule/`, `tests/watchdog/`) but most watchdog
  tests still live flat in `tests/` (10 `test_watchdog_*.py` files) — another
  half-finished regrouping.

---

## Executor rules (every executor of every phase MUST follow these)

1. Use long timeouts (**600000 ms**) for all test runs. **Never end a turn to "wait for
   tests"** — run them and read the result in the same turn (background + Monitor loop
   if needed).
2. **Confirm baseline green before changing anything.** Run the full suite first with the
   documented env (`_JUGGLE_TEST_DB="$HOME/.claude/juggle/juggle.db"`,
   `CLAUDE_PLUGIN_DATA="$HOME/.claude/juggle"`, `JUGGLE_MAX_BACKGROUND_AGENTS=5`,
   `JUGGLE_MAX_THREADS=10`, shared DB set up via `init-db`/`start` per CLAUDE.md). Fix
   pre-existing reds (or document the known active-state isolation failures) BEFORE any
   refactor commit.
3. **Harness smoke gate after every commit (repo CLAUDE.md gate):** full
   `uv run pytest -q` green AND `uv run python src/juggle_cli.py doctor --dry-run`
   smoke against a tmp DB, with the pytest summary line pasted in the commit body.
   Any commit touching cockpit/TUI files additionally runs
   `uv run src/juggle_cli.py cockpit --smoke --all-viewports` (all 7 viewport
   profiles in `config/viewports.yaml` must pass).
4. If a phase cascades into breakage you can't resolve, **revert to the last green
   commit**, record the revert + reason in `plan/2026-06-10-refactor-results.md`, and
   continue with the next phase. Never leave the repo red.
5. **Mechanical commits separate from behavior commits.** `git mv` / shim / rename
   commits contain zero logic edits.
6. **Regression-pin tests move verbatim, never weakened or deleted.** If a refactor makes
   a pin obsolete, rewrite it to assert the same behavior through the new seam (per the
   repo regression-pin gate; deletion requires explicit user approval).
7. DO NOT change behavior of entry points: `src/juggle_cli.py`, `src/juggle_hooks.py`,
   and `scripts/*` paths stay invocable exactly as `hooks/hooks.json` and the 26
   referencing plugin files expect, at every commit.
8. After each phase: `graphify update .` and bump plugin patch version per repo
   versioning rules on the phase's final commit.

---

## Phase 0 — Measurement + guardrail (no source changes)

**Steps**
1. Add `scripts/loc_gate.py`: walks git-tracked `src/**/*.py` (and `scripts/*` python
   scripts), fails (exit 1, listing offenders) on any module >300 lines unless it is in
   the grandfathered allowlist. Allowlist is a literal dict in the script
   `{path: line_budget}` seeded with the 22 current offenders at their CURRENT line
   counts — the gate also fails if a grandfathered file GROWS past its recorded budget.
   The allowlist may only shrink (entries removed / budgets lowered) — enforce by
   comment + code review, and each later phase commit lowers it.
   CLI: `--json`, `--update-baseline` (prints, never auto-writes).
2. Add `tests/test_loc_gate.py` invoking the gate as a subprocess (and unit-testing the
   allowlist-shrink invariant: every allowlist entry must currently exceed 300 lines —
   stale entries fail the test, forcing removal).
3. Record this plan's baseline table as the canonical "before" snapshot; create the
   skeleton of `plan/2026-06-10-refactor-results.md` with the before column filled.

**Commit boundary:** one commit (`feat: loc gate + baseline`).
**Risk:** none (additive).

## Phase 1 — Kill verified duplication (single source of truth)

Only the duplications verified in F2. Do not invent others.

1. **`src/daemon_pidfile.py`** (~80 lines): `write_singleton_pid(pidfile)`,
   `is_process(pid, cmdline_substr)`, `kill_existing_from_pidfile(pidfile,
   cmdline_substr, *, kill_group: bool, wait: bool)`. Behavior-preserving means
   preserving each caller's CURRENT semantics via flags (monitor: group-kill+wait;
   watchdog: plain SIGTERM) — unifying kill semantics is a behavior change and is OUT of
   scope (note it as follow-up). Rewire `scripts/juggle-agent-monitor`,
   `scripts/juggle-agent-watchdog`, and `src/juggle_watchdog.py:1039-1098` (the watchdog
   functions become thin delegating shims since tests/scripts import them by name).
   Commit 1.
2. **One `claude -p` core:** add `run_claude_p(prompt, *, model, timeout, output_format,
   cost_tracker=None)` in `src/llm_calls.py` (move `llm_call` there too from
   `juggle_cli_common.py`, leaving a re-export shim). Rewire
   `juggle_schedule_common.claude_p` (keeps its cost-tracking wrapper),
   `juggle_project_summary._claude_sonnet` (keeps name as shim — it's an injectable test
   seam), and the two inline call sites in `juggle_cmd_projects.py:447,686`. Commit 2.
3. **Consolidate schedule-common tests:** union `tests/test_juggle_schedule_common.py`
   into `tests/schedule/test_schedule_common.py` (keep every distinct assertion from
   both; collected-test count for the module must be ≥ max of the union, verified with
   `--collect-only`). Delete the flat file only after the union is proven. Commit 3.

**Risks:** monitor/watchdog kill-semantics regressions — covered by
`tests/test_agent_monitor.py`, `tests/test_watchdog*.py`; run those first, then full suite.

## Phase 2 — Decompose the god modules (behavior-preserving, shims)

One module per commit (or two commits for db). Pattern for every step: create new
modules → move code verbatim → original file becomes (or keeps) re-export shims so
`from juggle_db import JuggleDB` etc. keep working → suite green → lower loc_gate
allowlist entry.

1. **`juggle_db.py` (1962 → core <300 + mixins)** — split along the existing section
   comments into `src/db/` mixins: `db/core.py` (connection, schema, init, module-level
   helpers `_now`, `_next_excel_label`...), `db/threads.py` (thread ops + state machine),
   `db/projects.py`, `db/messages.py`, `db/notifications.py` (v1+v2 + shared context),
   `db/actions.py`, `db/agents.py` (agent pool), `db/selfheal.py` + archive.
   `JuggleDB(ThreadsMixin, ProjectsMixin, ...)` assembled in `db/core.py`;
   `src/juggle_db.py` becomes `from db.core import JuggleDB, DB_PATH ...` shim
   (~20 lines). Needs `src/db/__init__.py`; flat sys.path makes this import as top-level
   package `db` — name it `juggle_dbpkg` or `db` and verify no stdlib/site-package
   collision (prefer `dbops` to avoid any ambiguity). 2 commits (threads/projects first,
   rest second).
2. **`juggle_watchdog.py` (1332)** → `watchdog_classify.py` (pure functions:
   `classify_pane_state`, `_classify_agent_state`, `_strip_ansi`, `_hash_tail`,
   thresholds), `watchdog_recovery.py` (`execute_recovery`, `_handle_crashed`,
   `nudge_and_notify`, snapshots), `watchdog_restart.py` (hot-restart policy:
   `should_hot_restart`, `_maybe_hot_restart`, `_collect_mtimes`, cold-start tracking),
   `watchdog_inspect.py` (`inspect_agent`, `check_orphaned_threads`). Shim re-exports in
   `juggle_watchdog.py` are MANDATORY here — `scripts/juggle-agent-watchdog` imports 15+
   names from it and hot-restart watches that file's mtime. 1 commit.
3. **`juggle_cockpit.py` (1120)** → extract `juggle_cockpit_profile.py` (psrecord
   profiling, ~300 lines; `test_cockpit_profile.py` exists and pins it),
   `juggle_cockpit_layout.py` (ratio sanitize/clamp/persist helpers). `CockpitApp`
   actions that are >20 lines delegate into `_helpers`. Run viewport smoke matrix.
   1 commit.
4. **`juggle_cmd_agents.py` (1098)** → `juggle_cmd_agents_lifecycle.py`
   (spawn/get/release/decommission), `juggle_cmd_agents_tasks.py`
   (send_task/send_message/complete/fail + worktree helpers), keep
   actions/notify/watchdog ctl in the original. 1 commit.
5. **`juggle_hooks.py` (1056)** → keep the entry dispatcher (`main`, `handle_*` thin) in
   `juggle_hooks.py` (path is pinned by hooks.json), move bodies to
   `hooks_prompt.py` / `hooks_tooluse.py` (pre/post tool-use + telemetry + class-B scan) /
   `hooks_checkpoint.py` (checkpoint write/restore + pre-compact). 1 commit.
6. **`juggle_cli.py` (1006)** → it already delegates to `juggle_cmd_*`; move the inline
   command funcs (`cmd_cockpit`, `cmd_agent_tools`, selfheal cmds, vault/editor cmds)
   into `juggle_cmd_misc.py`/existing cmd modules so `juggle_cli.py` is parser wiring +
   env bootstrap only (<300). PEP-723 header and path stay. 1 commit.

**Risks:** monkeypatch targets in tests (`patch("juggle_watchdog.X")`) — shims must be
module attributes, and where tests patch the ORIGINAL module the moved code must look up
through the original module or tests must be updated mechanically in the same commit.
Grep `tests/` for `juggle_db.`, `juggle_watchdog.`, `juggle_hooks.` patch strings before
each step.

## Phase 3 — Domain subpackages (flat namespace → grouped)

Only after Phase 2 is green. Target layout under `src/` (plain dirs + `__init__.py`,
imported as top-level packages thanks to the existing sys.path bootstrap):

```
src/
  juggle_cli.py, juggle_hooks.py        # pinned entry points, stay put
  dbops/        # Phase 2 db split lands here directly
  watchdog/     # watchdog_* modules + health
  cockpit/      # juggle_cockpit*  (view/model/modals/helpers/widgets/profile/layout)
  cmds/         # juggle_cmd_*
  schedules/    # juggle_schedule_*
  corelib/      # settings, context, tmux, harness, hindsight, llm_calls, daemon_pidfile,
                # cli_common, scheduler, selfheal, smoke, research_*, ...
  harnesses/    # already exists
```

**Steps**
1. Pick non-colliding package names (avoid `db`, `hooks` shadows). `git mv` one domain
   per commit; leave `src/juggle_<old>.py` one-line re-export shims.
2. Migrate internal imports to the new paths (mechanical commit per domain), then update
   external references: `scripts/*` sys.path imports, any `commands/*.md` /
   `skills/*` that name non-entry-point src files (audit the 26 references from F1 —
   most point at `juggle_cli.py`/`juggle_hooks.py` which do not move; fix the rest,
   e.g. skill docs naming `juggle_schedule_*.py`).
3. Remove shims once `grep -rn "import juggle_<old>"` over src/tests/scripts is clean —
   one final mechanical commit. Update watchdog hot-restart `_collect_mtimes` to scan
   recursively (this is the one small behavior-adjacent edit; pin it with a test).

**Risks:** plugin runs from `${CLAUDE_PLUGIN_ROOT}` in user environments — entry-point
path stability is the hard constraint; everything else is internal. The `--smoke` +
`doctor --dry-run` harness gate after every commit covers wiring.

## Phase 4 — Library logic out of scripts; findable tests

1. Move the daemon main-loops of `scripts/juggle-agent-watchdog` and
   `scripts/juggle-agent-monitor` into `watchdog/daemon.py` / `watchdog/monitor.py`;
   scripts become argv→`main()` wrappers (<30 lines each, keep PEP-723 headers).
   `scripts/consolidate_dbs.py` / `measure_agent_compliance.py` likewise if they contain
   logic worth testing; otherwise leave (they are offline utilities). 1–2 commits.
2. Split mega test files by topic, move-only:
   - `test_juggle_cli.py` (1058) → per-command files (`test_cli_threads.py`,
     `test_cli_vault.py`, ...) under `tests/cli/`.
   - `test_juggle_db.py` (746) → `tests/db/` mirroring Phase 2 mixins.
   - `test_cockpit_keys.py` (1021) / `test_cockpit_features_v2.py` (738) →
     `tests/cockpit/test_cockpit_keys_*.py`, and rename `_features_v2` by actual topic
     (e.g. `test_cockpit_bell.py`, `test_cockpit_filter.py` per its test classes).
   - Move the 10 flat `tests/test_watchdog_*.py` into `tests/watchdog/` (finish the
     existing half-done regrouping); `test_watchdog_event_regression_2026_05_18.py`
     moves VERBATIM to `tests/watchdog/regressions/test_event_2026_05_18.py` — content
     untouched, docstrings keep incident dates.
   - Rename `*_v2` files to topic names.
   Invariant per commit: `pytest --collect-only -q` total count never decreases
   (1390 baseline; record count in each commit body). Regression pins byte-identical
   (verify with `git diff --find-renames --stat` showing 100% renames).
3. Mechanical commits only; one commit per test-domain move.

## Phase 5 — Navigation layer

1. One-paragraph purpose docstring at the top of every module in src packages (what it
   owns, what it must not own, key entry points). Mechanical sweep, 1 commit.
2. `docs/ARCHITECTURE.md` (1 page): package map, entry points + plugin wiring
   constraints (hooks.json, 26 reference files), data flow (CLI/hooks → dbops →
   tmux/watchdog → cockpit), where to add a new command/schedule/panel. Link it from
   repo `CLAUDE.md`. 1 commit.
3. `graphify update .`; confirm god-node edge counts dropped; paste new top-10 into the
   results report.
4. Write `plan/2026-06-10-refactor-results.md`: before/after table (LOC >300 count,
   top-10 LOC, god-node edges, test count, suite runtime), reverts taken, follow-ups
   (e.g. unify pidfile kill semantics — deferred behavior change).

---

## Acceptance criteria (numbers)

1. `src/` modules >300 lines: **22 → ≤3** (allowed residue: assembled `CockpitApp`
   module, `juggle_tmux.py` if its split proves risky, one cmd module — each with an
   explicit allowlist budget and a follow-up note).
2. loc_gate allowlist strictly smaller at end than start; gate runs in the test suite.
3. `JuggleDB` god-node edges **1392 → materially down** (target <600 for the largest
   single node after mixin split; re-measured via graphify).
4. Zero verified duplications remaining: one pidfile module, one `claude -p` core, one
   schedule-common test home.
5. Test count ≥ **1390** collected at every commit; full suite + `doctor --dry-run`
   smoke + (for cockpit commits) `--smoke --all-viewports` green at every commit, with
   summary lines in commit bodies.
6. All regression-pin tests present and unweakened (rename-only diffs).
7. Entry points unchanged: `hooks/hooks.json` and all 26 referencing plugin files work
   without edits except where the audit in Phase 3 step 2 deliberately updates them.
8. `docs/ARCHITECTURE.md` exists, linked from CLAUDE.md; every package module has a
   purpose docstring; `graphify-out/` regenerated at HEAD.
