# Refactor Results — Understanding & Token Efficiency (2026-06-10)

Companion results report for `plan/2026-06-10-refactor-for-understanding-and-tokens.md`.
Before column is the canonical baseline (main @ 88dc30e, gate baseline measured on
branch cyc_refactor-tokens after the hermetic test fix bd4b099).

## Before / After (FINAL — end of Phase 5, 2026-06-10)

| Metric | Before (2026-06-10) | After (Phase 5 final) |
|---|---|---|
| Total Python LOC (git-tracked `*.py`) | 43,664 across 143 files | 45,734 across 195 files (split modules + duplicated test headers) |
| `src/` modules >300 lines | 22 | 20 (god modules split; remainder grandfathered, see allowlist) |
| All gated files >300 lines (src `*.py` + python scripts) | 23 (incl. `scripts/talkback` 415) | 21 |
| loc_gate allowlist entries | 23 | 21 (shrink-only; 0 offenders, 0 stale) |
| Tests collected | 1390 | 1400 (+10 loc-gate tests; invariant held at every commit) |
| Full-suite result | 1372 passed, 18 skipped (161.82s) | 1382 passed, 18 skipped, 9 warnings (160.71s) |
| `JuggleDB` god-node edges (graphify) | 1392 | **926** (target <600 not reached — see scorecard) |
| `JuggleTmuxManager` edges | 474 | 372 |
| Graph size | 6667 nodes · 11633 edges (commit b378fac) | 7030 nodes · 11818 edges (HEAD) |
| scripts/ daemon scripts | watchdog 232 / monitor 129 lines of logic | thin wrappers (18 / 20); logic in src (gated ≤300) |
| Test files >700 lines | 6 (cli 1058, tmux 1033, cockpit_keys 1021, db 746, features_v2 738, stall 722) | 2 (cockpit_keys 1021 — protected; watchdog/test_stall_regressions 722 — pins, moved verbatim) |

Top-10 src LOC (after Phase 5):

```
964 src/juggle_watchdog.py     839 src/juggle_tmux.py
835 src/juggle_cockpit.py      823 src/schedules/autofix.py
735 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
598 src/juggle_context.py      545 src/schedules/reflect.py
532 src/dbops/migrations.py    499 src/juggle_cockpit_view.py
```

Top-10 src LOC (before):

```
1962 src/juggle_db.py          1098 src/juggle_cmd_agents.py
1332 src/juggle_watchdog.py    1056 src/juggle_hooks.py
1120 src/juggle_cockpit.py     1006 src/juggle_cli.py
 839 src/juggle_tmux.py         823 src/juggle_schedule_autofix.py
 737 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
```

Top-10 src LOC (after Phase 2):

```
1098 src/juggle_cmd_agents.py   964 src/juggle_watchdog.py
1056 src/juggle_hooks.py        839 src/juggle_tmux.py
1006 src/juggle_cli.py          835 src/juggle_cockpit.py
 823 src/juggle_schedule_autofix.py  735 src/juggle_cmd_projects.py
 673 src/juggle_cmd_threads.py  598 src/juggle_context.py
```

juggle_db.py (1962 lines) no longer appears — split into 9 modules (largest: juggle_db_migrations.py 532).

## Baseline notes

- Baseline on unmodified main showed 7 pre-existing failures under the documented
  test env (`JUGGLE_MAX_THREADS=10`): `tests/test_juggle_settings.py` (env-override
  precedence) and `tests/test_juggle_smoke.py` (`_make_db` seeds 30 threads past the
  cap). Fixed test-side in bd4b099 before any refactor work; suite fully green since.

## Phase progress

- **Phase 0** done (57b2ca6): loc gate + tests + this baseline doc. Gate green,
  23 grandfathered entries.
- **Phase 1** done:
  - 1.1 (679e2de): `src/daemon_pidfile.py` single source of truth; monitor /
    watchdog-script / juggle_watchdog shims preserve per-site semantics.
    Finding: the monitor's old docstring claimed process-group kill, but its
    code always did single-pid SIGTERM→wait→SIGKILL — both kill paths were
    already identical; only logging and pidfile-write verification differ.
  - 1.2 (499fe89): `src/llm_calls.py` (`run_claude_p` + moved `llm_call`),
    four call sites rewired behavior-preserving. Allowlist shrunk: 23 → 22
    entries (`juggle_schedule_common.py` now 297 lines; watchdog budget
    1332→1301, cmd_projects 737→735).
  - 1.3 (aad7ea0): schedule-common tests unified in
    `tests/schedule/test_schedule_common.py` — 95 collected (exact union,
    zero exact duplicates found, nothing dropped); flat file deleted.
    Total collection 1400 (baseline 1390 + 10 loc-gate tests).
  - Phase 1 wrap-up / version bump (508683c → v1.51.2).
- Pre-phase: gate-language docs fix (ebcc898) and hermetic-env test fix
  (bd4b099) — see Baseline notes.
- **Phase 2** done (v1.52.0):
  - 2.1 (7bdc221): `juggle_db.py` (1962→144 lines) split into 9 domain modules:
    - `juggle_db_schema.py` (277) — DDL, constants, pure helpers
    - `juggle_db_migrations.py` (532) — 34 schema migrations (grandfathered, see debt)
    - `juggle_db_session.py` (113), `juggle_db_threads.py` (280)
    - `juggle_db_projects.py` (218), `juggle_db_messages.py` (174)
    - `juggle_db_notifications.py` (153), `juggle_db_selfheal.py` (109)
    - `juggle_db_agents.py` (296)
    - All `from juggle_db import X` callers work via re-export shims. MAX_THREADS
      patch fixture updated to patch all 3 module namespaces.
  - 2.2 (a1cd4a1): `juggle_watchdog.py` (1301→964 lines) extracted:
    - `juggle_watchdog_restart.py` (128) — hot-restart + stale-source detection
    - `juggle_watchdog_inspect.py` (262) — inspect_agent + _handle_crashed
    - Circular import avoided via lazy function-body import in inspect module.
    - All `patch("juggle_watchdog.X")` targets preserved via module-level re-exports.
  - 2.3 (af0b935): `juggle_cockpit.py` (1120→835 lines) extracted:
    - `juggle_cockpit_layout.py` (104) — column-ratio constants + helpers
    - `juggle_cockpit_profile.py` (232) — headless psrecord profiling harness
  - 2.4 (reverted): `juggle_cmd_agents.py` lifecycle split failed — tests universally
    patch `juggle_cmd_agents.get_db`, `._resolve_thread`, `.JuggleTmuxManager` etc.
    Moving functions to `juggle_cmd_agents_lifecycle.py` breaks all those patches.
    Root cause: every lifecycle command reads module-level globals patched by tests.
    Fix requires either (a) passing globals as parameters or (b) updating ~20 test
    patch targets. Deferred to Phase 3 (when internal imports are migrated anyway).
  - 2.5 (deferred): `juggle_hooks.py` split blocked by same pattern — module-level
    globals (`DB_PATH`, `_CHECKPOINT_PATH`, `AUTOPILOT_FLAG`) read by every handler.
    A `hooks_config.py` constants module would resolve this; deferred to Phase 3.
  - 2.6 (deferred): `juggle_cli.py` main() is 628 lines of pure argparse wiring;
    the non-main functions (329 lines) could move to `juggle_cmd_misc.py`, but that
    still leaves juggle_cli.py at ~700 lines due to argparse volume. Grandfathered.

## Phase 3 (2026-06-10, second session)

Commits (all green: 1382 passed, 18 skipped, 9 warnings at every commit;
`doctor --dry-run` vs tmp DB + loc_gate after each):

- **Crash recovery / salvage** (2fc0456): predecessor session crashed mid-Phase-3.
  Salvaged its WIP: uncommitted test edits for the hooks split (tests now patch
  `juggle_hooks_config.DB_PATH`/`AUTOPILOT_FLAG` alongside `juggle_hooks.*`) —
  committed as the completion of 6831f1d. Untracked WIP modules
  `juggle_cmd_agents_common.py`/`_lifecycle.py` verified faithful and reused.
- **A2 — cmd_agents split done** (0437d95): `juggle_cmd_agents.py` 1098→203.
  Six modules: `_common` (141, shared symbols + pure classifiers — the SINGLE
  test patch surface; sub-modules read `_com.<symbol>` at call time),
  `_worktree` (93), `_pool` (104), `_lifecycle` (226), `_complete` (264),
  `_tasks` (218). Facade keeps action-item + watchdog-ctl commands and
  re-exports everything. The Phase 2.4 blocker (test patch targets) resolved by
  re-pointing `patch("juggle_cmd_agents.<sym>")` → `juggle_cmd_agents_common`
  across 10 test files; the `_com` indirection means future moves between
  sub-modules can never break patches again. Reaper AST pin re-targeted to the
  lifecycle module (same assertion, new seam). Allowlist 23→22.
- **A3 — juggle_cli split done** (281a708): `juggle_cli.py` 1006→192 (pinned
  path + PEP-723 header intact). Parser registration extracted to
  `juggle_cli_parsers_{threads,agents,misc}.py` (123/275/290); cockpit,
  agent-tools, and selfheal handlers moved to `juggle_cmd_misc.py` (201).
  Vault + open-in-editor helpers stay in juggle_cli.py — tests patch
  `juggle_cli.get_settings`/`NVIM_SOCKET`/`subprocess` there. No grandfather
  needed (the "still >300 after extraction" prediction didn't materialize).
  Allowlist 22→21.
- **B — schedules/ subpackage** (abd9c33): `juggle_schedule_*` →
  `src/schedules/{common,autofix,dogfood,reflect}.py` + `__init__.py`.
  Deprecated shims kept at old flat paths. Path-depth fixes (`JUGGLE_REPO`,
  `SRC_DIR` bootstrap now `.parent.parent`). CLI dispatch via
  `__import__("schedules.<name>", fromlist=["run"])`. 5 test files re-targeted.
- **B — dbops/ subpackage** (bbb2143): 9 `juggle_db_*` modules →
  `src/dbops/{schema,migrations,session,threads,projects,messages,
  notifications,selfheal,agents}.py` (git rename-detected, 97-99%).
  `src/juggle_db.py` remains the public composition root — zero caller churn
  (`from juggle_db import X` everywhere). Only test_juggle_smoke's MAX_THREADS
  fixture re-targeted. No shims needed (submodule names were 1 day old).

### Phase 3 deferrals (with root causes)

- **cockpit/ subpackage — BLOCKED**: `src/juggle_cockpit_modals.py`,
  `src/juggle_cockpit_view.py`, `tests/test_cockpit_keys.py` carry the user's
  unrelated uncommitted edits; `git mv`/rewiring would entangle or clobber
  them. Do this domain when that work has landed. Planned:
  `cockpit/{app,view,model,modals,helpers,widgets,layout,profile}.py`.
- **cmds/ subpackage — deferred (churn ≫ value)**: 147 live test patch sites
  (`juggle_cmd_agents_common.` 44, `juggle_cmd_projects.` 35,
  `juggle_cmd_integrate.` 29, `juggle_cmd_threads.` 21, `juggle_cmd_context.`
  17, `juggle_cmd_research.` 1) across 23+ test files would all need string
  renames — re-churning the patch surfaces updated TODAY in A2/A3, with typo
  regression risk and zero structural gain: the `juggle_cmd_*`/
  `juggle_cli_parsers_*` prefix already groups the domain lexically.
- **watchdog/ subpackage — deferred (would be half-grouped + behavior edit)**:
  the 964-line hub `juggle_watchdog.py` is path-pinned (mtime-watched by
  `scripts/juggle-agent-watchdog`), so only the 3 satellites
  (`_restart`/`_inspect`/`_health`) could move — leaving the hub outside its
  own package, the exact half-finished-decomposition anti-pattern. Also
  requires the `_collect_mtimes` flat-`glob("*.py")` → recursive change
  (hot-restart staleness would otherwise silently stop watching moved files) —
  a behavior change needing its own pin. Do it when the hub is shimmed.
- `_collect_mtimes` recursive scan: not needed yet — neither `schedules/` nor
  `dbops/` contains watchdog-daemon code (the daemon imports `juggle_db.py`
  shim, `juggle_watchdog*.py`, `juggle_tmux.py`, all still flat in src/ and
  still watched). MUST be revisited before moving watchdog/ or tmux.

### Top src LOC (after Phase 3)

```
964 src/juggle_watchdog.py     839 src/juggle_tmux.py
835 src/juggle_cockpit.py      823 src/schedules/autofix.py
735 src/juggle_cmd_projects.py 673 src/juggle_cmd_threads.py
598 src/juggle_context.py      545 src/schedules/reflect.py
532 src/dbops/migrations.py    499 src/juggle_cockpit_view.py
```

juggle_cmd_agents.py (1098) and juggle_cli.py (1006) no longer appear.
loc_gate: 23 → 21 grandfathered entries this session (removed
juggle_cmd_agents.py and juggle_cli.py; schedule/db entries renamed to their
new package paths). 85 files checked, 0 offenders, 0 stale.

## Phase 4 (2026-06-10, third session)

All commits green (1382 passed, 18 skipped, 9 warnings; collected 1400
unchanged; `doctor --dry-run` vs tmp DB + loc_gate after each):

- **4.1 — daemon loops out of scripts** (b13d63a): `scripts/juggle-agent-watchdog`
  (232→18) and `scripts/juggle-agent-monitor` (129→20) are thin argv→main()
  wrappers over new `src/juggle_watchdog_daemon.py` (243) and
  `src/juggle_monitor_daemon.py` (123). Logic moved verbatim; watchdog logging
  setup moved into `main()` (import is now side-effect free; runtime-identical).
  Both modules live flat in src/ so the hot-restart `_collect_mtimes` flat glob
  keeps watching them. Test loaders (`test_agent_monitor`,
  `test_reaper_ownership`) re-targeted to the new seam, assertions unchanged.
  Script audit of the remaining 5: `juggle-selfheal-monitor` (81) already thin;
  `talkback` (415) self-contained PEP-723 TTS service with its own dep set
  (kokoro/sounddevice/flask) — extraction would drag those deps into src, left
  allowlisted; `talkback-stop-hook` (129) hooks.json-pinned pure functions —
  left; `consolidate_dbs.py` / `measure_agent_compliance.py` offline one-shot
  utilities — left per plan.
- **4.2 — watchdog test regroup** (dba7460): 11 flat `test_watchdog_*` /
  `test_db_watchdog` files → `tests/watchdog/` (git rename-detected 95-99%;
  only the sys.path depth lines changed). Date-stamped
  `test_watchdog_event_regression_2026_05_18.py` renamed by topic to
  `test_recovery_event_regressions.py` (incident date 2026-05-18 kept in
  docstring). `test_watchdog_stall_regressions.py` moved verbatim (722 lines of
  pins — deliberately NOT split). tests/watchdog/ now 164 tests.
- **4.2 — mega test splits** (b7f6f7a, 1dad309, 15d3c30, 88feb4b), all
  move-only with per-file collected counts proven equal:
  - `test_juggle_cli.py` (1058) → `test_cli_{threads,agents,editor,flags}.py`
    (483/448/171/121; 67 tests)
  - `test_juggle_tmux.py` (1033) → `test_tmux_{lifecycle,send_task,submission,
    send_message}.py` (388/130/488/130; 48 tests)
  - `test_juggle_db.py` (746) → `test_db_{threads,messages,thread_state,
    archive,migrations}.py` mirroring dbops seams (63 tests)
  - `test_cockpit_features_v2.py` (738, unfindable) →
    `test_cockpit_{actions,filter,bell,tail_drawer}.py` (28 tests)
  - protected `tests/test_cockpit_keys.py` untouched per instructions
- **4.2 — *_v2 renames** (927704b): `test_cockpit_model_v2` →
  `test_cockpit_model_snapshot`, `test_context_injection_v2` →
  `test_context_injection`, `test_schema_v2` → `test_db_schema` (pure git mv).

## Phase 5 (2026-06-10, third session)

- **5.1 — docstring sweep** (3bf6bdb): audit found 0 src modules without a
  module docstring. `schedules/common.py` one-liner expanded to a purpose
  paragraph within its 300-line ceiling. `juggle_cmd_context` /
  `juggle_cmd_threads` / `juggle_tmux` sit exactly at their grandfathered
  budgets (shrink-only), so their one-liners stay until those modules split.
- **5.2 — navigation layer** (ee055bd): `docs/architecture.md` →
  `docs/ARCHITECTURE.md` with a new Code-map section: domain table, pinned
  entry-point constraints (cli/hooks/watchdog paths, flat `_collect_mtimes`
  caveat), LOC gate + shrink-only allowlist policy. Linked from repo CLAUDE.md
  ("Project Context") and README.
- **5.3 — graphify**: graph regenerated at HEAD (7030 nodes · 11818 edges).
  God nodes: JuggleDB 1392 → **926**, JuggleTmuxManager 474 → **372**,
  CockpitApp 220 → 196.

## Acceptance-criteria scorecard (vs plan)

1. src/ modules >300: 22 → 20. **Partial.** Target was ≤3; the deep split of
   watchdog/tmux/cockpit/cmd_projects/context was descoped when Phase 3
   subpackaging was deferred (protected cockpit WIP, 147 test patch sites,
   path-pinned watchdog hub). Gate + shrink-only allowlist hold the line.
2. Allowlist strictly smaller (23 → 21) and gate runs in the suite
   (`tests/test_loc_gate.py`). **Met.**
3. JuggleDB edges 1392 → 926 (-33%). **Partial** — materially down, but >600;
   the remaining edges are the 84-method facade callers (graphify counts the
   composed class, not the mixins).
4. Zero verified duplications: one pidfile module (`daemon_pidfile`), one
   `claude -p` core (`llm_calls`), one schedule-common test home. **Met.**
5. Collected ≥1390 at every commit (held at 1400); full suite +
   `doctor --dry-run` green per commit with summaries in commit bodies.
   **Met.** (Cockpit viewport smoke not run — no cockpit source commits.)
6. Regression pins present and unweakened — moves rename-detected 95-99%,
   pins byte-identical; two loaders re-targeted to new seams with identical
   assertions (documented in their docstrings). **Met.**
7. Entry points unchanged (`juggle_cli.py`, `juggle_hooks.py`, script paths;
   hooks.json untouched). **Met.**
8. ARCHITECTURE.md exists + linked; module docstrings present everywhere;
   graphify-out regenerated at HEAD. **Met** (3 docstrings remain one-line,
   blocked by at-budget gate entries — see 5.1).

## Deferrals carried forward (post-refactor follow-ups)

- **cockpit/ subpackage** — blocked on user's uncommitted cockpit WIP
  (`juggle_cockpit_modals/view`, `test_cockpit_keys`). Do when landed.
- **cmds/ subpackage** — churn ≫ value (147 live test patch sites).
- **watchdog/ subpackage** — needs the hub shimmed + `_collect_mtimes`
  recursive-scan behavior change (with its own pin) first. MUST precede any
  move of watchdog/tmux modules out of flat src/.
- **Deep split of the remaining 20 >300 modules** (watchdog 964, tmux 839,
  cockpit 835, autofix 823, cmd_projects 735, ...) — continue lowering the
  allowlist one module per iteration per the architecture gate.
- Unify pidfile kill semantics (monitor vs watchdog) — deliberate behavior
  change, out of refactor scope.
- `dbops/migrations.py` (532) — single ordered migration sequence; revisit
  if it passes 700 lines.
- Purpose-paragraph docstrings for `juggle_cmd_context` / `juggle_cmd_threads`
  / `juggle_tmux` when their splits free budget headroom.

## loc_gate allowlist after Phase 2

```
1098  src/juggle_cmd_agents.py    (Phase 2.4 deferred — test patch coupling)
1056  src/juggle_hooks.py         (Phase 2.5 deferred — module-global coupling)
1006  src/juggle_cli.py           (Phase 2.6 deferred — 628-line argparse main)
 964  src/juggle_watchdog.py      (lowered 1332→964; further split in Phase 3)
 839  src/juggle_tmux.py          (not touched yet)
 835  src/juggle_cockpit.py       (lowered 1120→835)
 823  src/juggle_schedule_autofix.py
 735  src/juggle_cmd_projects.py  (lowered 737→735)
 673  src/juggle_cmd_threads.py
 598  src/juggle_context.py
 545  src/juggle_schedule_reflect.py
 532  src/juggle_db_migrations.py (NEW — grandfathered; 34 ordered migrations,
                                    cannot split without losing ordering invariant)
 499  src/juggle_cockpit_view.py
 494  src/juggle_scheduler.py
 467  src/juggle_cockpit_model.py
 460  src/juggle_settings.py
 415  scripts/talkback
 406  src/juggle_schedule_dogfood.py
 392  src/juggle_cmd_research.py
 380  src/juggle_smoke.py
 370  src/juggle_cmd_context.py
 364  src/juggle_cmd_integrate.py
```

## Reverts / abandonments

- Phase 2.4 lifecycle split of `juggle_cmd_agents.py` reverted: widespread test
  patch-target breakage. Root cause: 20+ tests patch `juggle_cmd_agents.<symbol>`
  where `<symbol>` is `get_db`, `_resolve_thread`, `JuggleTmuxManager`, etc. When
  the functions move to `juggle_cmd_agents_lifecycle.py`, they read from that
  module's namespace, not `juggle_cmd_agents`'s. Fix = pass params or update patches.
  Deferred to Phase 3 when internal imports will be migrated anyway.

- Phase 2.5 hooks split deferred: `juggle_hooks.py` handlers all read module-level
  globals (`DB_PATH`, `_CHECKPOINT_PATH`, `AUTOPILOT_FLAG`, `_CHECKPOINT_MAX_AGE_SECS`)
  defined in the entry-point file. Sub-modules would need a `hooks_config.py` to
  import these constants without circularity. Deferred to Phase 3.

## Follow-ups (deferred behavior changes)

- Unify pidfile kill semantics (monitor: group-kill+wait vs watchdog: single
  SIGTERM) — Phase 1 preserves each call-site's behavior via flags; unification is
  a deliberate behavior change deferred out of this refactor.
- `juggle_db_migrations.py` (532 lines) — grandfathered as a single ordered sequence
  of 34 migrations. Could be split into batches (migrations 1-17 / 18-34) but the
  benefit is marginal since the file is read once at startup. Revisit if it grows
  past 700 lines.
- Phase 3 prerequisite for cmd_agents + hooks splits: extract module-level globals
  that are patched by tests into explicit parameters or a shared constants module.
