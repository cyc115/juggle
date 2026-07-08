# Refactor Scan — juggle `src/` — 2026-07-07

Weekly architecture-gate scan. **Scan & report only — no code changed.**
Repo: `/Users/mikechen/github/juggle` · branch `main` · 330 `src/**.py` files, 53.7k LOC.

## TL;DR

- **LOC gate is GREEN.** `scripts/loc_gate.py` → `OK — 347 files checked, 24 grandfathered`. Zero hard offenders; every module >300 lines sits within its pinned budget. This scan ranks **grandfathered debt paydown**, not violations.
- **Best value signal = at-budget × churn.** 11 grandfathered files are *at or within 1–6 lines of* their budget → the next edit that touches them is **blocked** until they split. Cross that with 90-day commit churn and the top targets fall out cleanly.
- **Top 3 to hand the architect:** (1) `juggle_settings.py` — extract the ~290-line `DEFAULTS` dict (churn 69, at budget, ~S effort); (2) `juggle_cockpit.py` — break the 1074-line `CockpitApp` god-class into action mixins (churn 94, 1-line headroom); (3) `juggle_watchdog.py` — extract the **331-line** `execute_recovery` fn (a >300 module nested inside a 1043-line file, at budget).
- **Dead code:** only 2 real lines (unused tmux re-export aliases). **Duplication:** 1 clean rule-of-three (`_find_or_create_schedule_thread`).

## Ranked findings (value / effort)

| # | file:line | Issue | Suggested refactor | Effort | Why now (value) |
|---|-----------|-------|--------------------|--------|-----------------|
| 1 | `src/juggle_settings.py:25` | ~290-line `DEFAULTS` config registry mixed with resolver logic (`_deep_merge`, `get_settings`, `get/get_nested`, `resolve_max_agents`). File 460L. | Move the `DEFAULTS` dict → new `juggle_settings_defaults.py`; keep resolvers here (drops to ~170L). | **S** | **Highest churn in repo (69 commits/90d)** and **at budget (460=460)** — every new config key is currently blocked. Mechanical move. |
| 2 | `src/juggle_cockpit.py:115` | `CockpitApp` god-class, 1074L / 40+ methods, 4 intermixed concerns: nav/scroll (`action_scroll_*`, `_cycle_pane*`), watchdog control (`_ensure_watchdog`, `action_watchdog_*`), status updaters (spool/version/mouse), thread mutations (`action_ack/close/archive/decommission`). | Extract action-handler mixins following the existing `GraphModeMixin` pattern: `CockpitNavMixin`, `CockpitWatchdogMixin`, `CockpitThreadActionsMixin`. | **L** | **Churn 94 (top), only 1 line of budget headroom** → effectively blocked. Seams are clean; mixin pattern already established. |
| 3 | `src/juggle_watchdog.py:513` | `execute_recovery` is **331 lines** — a >300 "module" inside a 1043L file. `check_orphaned_threads` (843) adds 178L. Plus 8 pane-classify predicates (`_strip_ansi`, `_hash_tail`, `_has_*`, `classify_pane_state`). | Extract `execute_recovery` → `juggle_watchdog_recovery.py`; classify predicates → `juggle_watchdog_classify.py`. **Keep FLAT `src/*.py`** — daemon hot-restart globs flat src only (ARCHITECTURE.md). | **L** | **At budget (1043=1043)**, safety-critical, churn 38. Kills a nested 331-line fn. |
| 4 | `src/juggle_tmux.py:674` | Module-level agent reconcile/reap (`oneshot_agent_alive`, `reconcile_oneshot_agents` 93L, `reap_stale_agents`) bolted onto a tmux-pane-manager (867L). Distinct concern. | Extract the 3 fns → new flat `juggle_tmux_reconcile.py` (separate from process-reaper `juggle_reaper.py` and DB-row reaper `juggle_agent_reap.py`). | **M** | Churn 63 (3rd-highest). Clears a whole concern out of a 2nd-biggest module. |
| 5 | `src/schedules/autofix.py:812` + `src/schedules/dogfood.py:269` | **Rule-of-three duplication:** `_find_or_create_schedule_thread` copy-pasted in both, bodies **already drifted** (dogfood adds `title LIKE 'schedule%'` preference; autofix doesn't). reflect.py also files to a schedule thread. | Hoist one canonical impl into `schedules/common.py` (already the shared-plumbing home for `CostTracker`, `gh_*`, `git_*`, `db_query`). | **S** | Kills a live drift-bug risk and frees lines in two at/near-budget schedule files. |
| 6 | `src/juggle_cmd_projects.py:233` | Pure drift/vector math (`drift_score`, `_build_vocab`, `_topics_to_bow_vector`, `check_and_resynth_if_drifted` ~75L) + LLM classifier-prompt builders (`_build_classifier_prompt`, `build_match_profile_prompt`) mixed with project CRUD handlers. 732L. | Extract vector/drift math → `juggle_project_drift.py`; classifier prompts → `juggle_project_classify.py`. | **M** | Churn 30. Two self-contained, testable seams (math has no I/O). |
| 7 | `src/juggle_cockpit_modals.py:528` | 5 modal classes + 3 **non-modal** data resolvers (`build_summary_ctx`, `resolve_task_detail`, `resolve_thread_detail`) in one 723L UI file. | Split heavy modals (`_TailModal`, `_ProjectArmModal`, `_HelpModal`) to own files; move the 3 data resolvers out of the UI-modal module. | **M** | Churn 31. Mixes data-resolution with view. |
| 8 | `src/juggle_cmd_threads.py:90` | Session lifecycle (`cmd_start`/`cmd_stop`/`_maybe_start_talkback`/`_start_watchdog_for_cmd_start`) + `_render_briefing` (~100L) + thread CRUD in 590L. | Extract session-lifecycle cmds → `juggle_cmd_session.py`; briefing render → `juggle_briefing.py`. | **M** | Churn 39. Three separable concerns. |
| 9 | `src/juggle_cockpit_view.py` (458/461) · `src/juggle_graph_dispatch.py` (349/355) | **Near-budget watchlist** — high churn, ≤6 lines headroom. Not mixed-concern enough to force a split yet, but the next feature breaches budget. | Pre-emptive: earmark an extraction target now (view: render vs. event-handling; graph_dispatch already split flat-fallback in 2026-07-04). | **M** | Churn 41 / 42. Flag so the architect isn't surprised mid-feature. |

## Dead code (`vulture --min-confidence 80`)

3 hits total — signal is very clean.

| file:line | Symbol | Verdict |
|-----------|--------|---------|
| `src/juggle_tmux.py:47-48` | `_input_box_has_content`, `_input_box_stuck` (aliased re-imports of `juggle_paste_submit` symbols) | **Real dead code.** The underscore aliases are referenced nowhere (grep of `src/` + `tests/` finds only the import). `wait_for_submission` uses `_classify_pane_submission`, not these. **Effort XS** — delete the two alias lines. *Verify first:* confirm no external harness patches `juggle_tmux._input_box_*`. |
| `src/juggle_cmd_agents_common.py:23` | `_last_sentences` | **False positive — no action.** It is an intentional `noqa: F401` re-export actively consumed by `juggle_cmd_agents.py:42`, `juggle_cli.py:100`, and tested in `test_cli_threads.py`. Vulture can't see the re-export patch-surface. |

## Not-a-finding (checked, clean)

- **git access is centralized** — zero `subprocess([...,"git",...])` call sites outside the `vcs`/`schedules.common` modules.
- **`CostTracker` already shared** — single class in `schedules/common.py`; the per-routine `_st()`/`_over_overall_cap()` wrappers are orchestration, not duplication.
- **`sys.path.insert` in 15 entry modules** — load-bearing bootstrap that must run before imports resolve; not extractable, intentional (ARCHITECTURE.md).

## Method & sources (retrieved 2026-07-07, local repo `main`)

- **Oversize:** `find src -name '*.py' | xargs wc -l | sort -rn` cross-referenced against the pinned budgets in `scripts/loc_gate.py` `GRANDFATHERED` (24 entries) and the live gate run (`loc_gate: OK`).
- **Value ranking:** `git log --since='90 days ago' --name-only -- 'src/*.py' | sort | uniq -c` (commit churn) × at-budget status. High churn + at-budget = next edit blocked = highest urgency.
- **Mixed-concern:** `grep -nE '^(class|def|    def)'` outlines of each top target (seam boundaries above are the actual line numbers).
- **Dead code:** `uv run --with vulture vulture src/ --min-confidence 80`, each hit grep-verified against `src/` + `tests/`.
- **Duplication:** grep of shared helper names across `src/schedules/*.py`, bodies diffed by eye.
- **Gate/seam policy:** `CLAUDE.md` (Architecture gate) + `docs/ARCHITECTURE.md` (LOC gate, domain seams, flat-src hot-restart constraint).

*Facts: line counts, churn, budgets, vulture output, grep results are measured. Opinions: the specific split shapes and effort estimates (S ≈ mechanical move; M ≈ 1 module + tests; L ≈ multi-module, high merge-risk) are the scanner's judgment for the architect to weigh.*
