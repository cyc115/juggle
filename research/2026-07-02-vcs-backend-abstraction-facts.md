# VCS-Backend Abstraction — Facts Doc (grounding, not a spec)

**Date:** 2026-07-02 · **Thread:** BZ · **Repo:** `~/github/juggle` @ 1.95.0
**Scope:** Ground the three sketched design decisions (VcsBackend abstraction,
stack-relative base ref, worktree-per-chain) in this repo's *actual* code before
a spec is written. Facts only — file:line references, no proposed interface.

> **Read this first — three hypotheses turned out partly WRONG.** See
> [§0 Corrections](#0-corrections-to-the-hypotheses) before designing.

---

## 0. Corrections to the hypotheses

| # | Hypothesis as stated | Reality in code | Impact |
|---|---|---|---|
| **H1** | "All git-specific shellouts should sit behind one interface" (implying none exists) | A VCS abstraction **already exists**: `src/vcs.py` — a `VCS` Protocol with `GitVCS` + **`HgVCS`** (Mercurial!) backends and a `detect()` resolver. BUT it is *tiny* (only `head`/`is_dirty`/`make_safety_branch`, used solely by the runs-ledger/restore path) and the entire integrate/worktree/verify machinery **bypasses it** with raw `subprocess.run(["git", ...])`. | The abstraction seam is already named and has a Mercurial precedent to extend — don't invent a new one, **widen `vcs.py`**. ~40 raw git call sites still route around it (§A). |
| **H2** | "Every `cyc_<thread>` worktree branches off `origin/main`." | **WRONG at creation.** `git worktree add -b <branch> <path>` (`juggle_cmd_agents_worktree.py:125`) passes **no commit-ish**, so the branch forks from the **current HEAD of the source repo's main worktree** (local `main`, whatever is checked out), *not* `origin/main`. The branch is only *trued up* to `origin/main` later, by the **rebase** step inside integrate (`juggle_cmd_integrate.py:224`, target resolved at `:179`). | "Change the default base-ref resolver" is really **two** insertion points: the `worktree add` commit-ish (currently implicit HEAD) AND the integrate rebase target. A stack-relative base must be threaded to both. |
| **H3** | "Worktree-per-node today; propose worktree-per-chain." | **Already worktree-per-*topic*, not per-node.** The 3-tier model (Project → Topic → Task-DAG) makes the **Topic** the unit: `schema_graph.py:34` "ONE thread/agent/worktree per topic; integrate runs once per topic"; `graph_scheduler.py:5` "one topic = one thread = one agent; **tasks are sequential**"; `graph_tick` "claims TOPICS (R9)" (`juggle_graph_dispatch.py:167-173`). Tasks inside a topic run **sequentially into the topic's single worktree** and integrate **once** at the topic tail. | The "collapse a 1-in-1-out chain into one worktree, integrate once at the tail" idea **already exists** — it's the Topic. The open question is whether "worktree-per-chain" is a *new* mechanism or a **base-ref/topic-boundary policy change** on the existing topic seam. Degree-based chain collapse would operate on **topic** in/out degree (`derived_topic_deps`), not task degree. |

Everything below is the supporting inventory.

---

## A. Git call-site inventory

Grouped by the VcsBackend method each would become. All are raw
`subprocess.run(["git", ...])` **except** where noted as already inside `vcs.py`.
`file:line` is the git invocation line.

### A0. Already behind the abstraction (`src/vcs.py`) — the existing seam
- `GitVCS.head` — `vcs.py:46` `git rev-parse HEAD`
- `GitVCS.is_dirty` — `vcs.py:49` `git status --porcelain`
- `GitVCS.make_safety_branch` — `vcs.py:53` `git branch <name> <sha>`, `:55` `git switch <name>`
- `HgVCS` (Mercurial precedent) — `vcs.py:63` `hg id -i`, `:67` `hg status`, `:71-73` `hg update -r` + `hg bookmark`
- `detect()` — `vcs.py:83-88` `.git` exists / `git rev-parse --is-inside-work-tree` / `.hg` exists / `hg root`
- `get_backend()` — `vcs.py:94-98` maps `"git"|"hg"` → instance
- **Consumers of `vcs.py` today:** only the runs-ledger / per-task VCS restore (`dbops/runs.py`, `juggle_cmd_runs.py`). NOT integrate, NOT worktree, NOT the verified gate.

### A1. `create_worktree` (the method that HARD-FAILS on EdenFS/Sapling today)
- `juggle_cmd_agents_worktree.py:125` — `git worktree add -b <branch> <path>` **← the exact call that fails on a `.git`-less EdenFS checkout** (no commit-ish → forks local HEAD; see H2)
- `juggle_cmd_agents_worktree.py:88` — `git worktree list --porcelain` (resolve MAIN worktree root; `_main_worktree_root`)
- `juggle_cmd_agents_worktree.py:115-117` — path/branch naming (`juggle-<basename>-<label>`, `cyc_<label>`)
- Call chain into it: `juggle_dispatch_core.py:185-186` `_create_worktree(repo_path_wt, thread_label_wt, DEFAULT_WORKTREE_ROOT)`, gated on `_role in ("coder","planner")` (`:163`), base resolved by `resolve_worktree_base` (`:178`)
- `DEFAULT_WORKTREE_ROOT = os.environ.get("JUGGLE_WORKTREE_ROOT", "/tmp")` — `juggle_dispatch_core.py:20`

### A2. `remove_worktree` / branch cleanup
- `juggle_cmd_agents_worktree.py:47` `git merge --ff-only <branch>`, `:58` `git worktree remove`, `:66` `git branch -d` (`_finalize_worktree`)
- `juggle_cmd_integrate.py:303` `git worktree remove --force` (PR mode), `:393` `git worktree remove --force`, `:397` `git branch -d` (direct mode)

### A3. `integrate` pipeline (the fetch→rebase→test→ff-merge→push machine)
All in `juggle_cmd_integrate.py::_run_integrate`, in execution order:
- `:52` `git status --porcelain` (dirty gate, `is_worktree_dirty`)
- `:62` `git rev-list --count <target>..<branch>` (empty-branch guard, `branch_commits_ahead`)
- `:147` `git status --porcelain` (dirty file list for the refuse message)
- `:159` `git rev-parse --git-dir` + `:167` `git rebase --abort` (idempotent abort of in-progress rebase)
- `:173` `git fetch --prune`
- `:181` `git rev-parse --verify <candidate>` — **rebase-target resolution**, tries `origin/main → origin/master → main → master` (`:179`)
- `:218` `git config merge.ours.driver true` (graphify-out merge driver)
- `:224` `git rebase <rebase_onto>` — **the true-up to trunk**; `:229` `git diff --name-only --diff-filter=U` (conflict list); `:234` `git rebase --abort`
- `:264` `git diff --stat <onto>..HEAD` (diffstat capture, best-effort)
- `:275` `git symbolic-ref --short HEAD` (resolve local main branch name)
- `:295` `git push origin <branch>:<branch> --force-with-lease` (**push_mode=="pr"**)
- `:323` `git checkout -- graphify-out/` + `:327` `git clean -fd -- graphify-out/`
- `:343` `git status --porcelain --untracked-files=no` (tracked-dirty guard before sync)
- `:351` `git merge --ff-only <rebase_onto>` (forward-only sync) OR `:361` `git reset --hard <rebase_onto>` (sync local main to base)
- `:367` `git merge --ff-only <branch>` (**the ff-merge into local main**)
- `:375` `git push origin <main>:<main>` (**push_mode=="direct"**)
- push modes: `direct` (ff-merge local main + push), `pr` (push branch only, no local merge), `none` (local ff-merge, no push) — `get_repo_config(main_repo_path)["push_mode"]`, `:92-93`
- **Success/failure:** any non-zero return → `_fail()` (`:131`) which files a HIGH `manual_step` action item and preserves branch+worktree. Runs entirely under a serialized per-repo lock (`acquire_repo_lock`, `:119`).

### A4. `is_ancestor` / `current_rev` — the **verified ⟺ merged** gate
- `dbops/graph_guards.py:37` `git -C <cwd> <args>` (generic `_git_ok` wrapper)
- `dbops/graph_guards.py:60` `git rev-parse --verify <branch>`, `:62` `git merge-base --is-ancestor <branch> <main>` (`branch_merged_to_main`)
- `dbops/graph_guards.py:72` `git rev-parse --verify <branch>` (`resolve_branch_sha`)
- `dbops/graph_guards.py:88` `git merge-base --is-ancestor <sha> <main>` (`sha_is_ancestor` — **THE single source of truth for `verified`**, see §C)
- `juggle_integrate_mergedsha.py:33` `git rev-parse <ref>`, `:42` `git cat-file -e <sha>`, `:63` `git merge-base --is-ancestor <sha> <canonical>` (records `merged_sha` only if it's a real ancestor)
- `juggle_repo_binding.py:145` `git fetch origin <branch>`, `:150` `git rev-parse --verify <candidate>` (`canonical_main_ref`)

### A5. `current_rev` / code-version fingerprint (watchdog)
- `juggle_watchdog_restart.py:37` `git rev-parse HEAD` (stale-code exit fingerprint; assumes canonical main worktree ff's on every integrate)
- `juggle_watchdog_singleton.py:162` `git worktree list --porcelain`

### A6. Repo-binding / toplevel resolution
- `juggle_repo_binding.py:29` `git rev-parse --show-toplevel` (`_git_toplevel`)
- `juggle_repo_binding.py:167` `git worktree list --porcelain` (`main_worktree_of`)
- `juggle_cmd_graph.py:57` `git rev-parse --show-toplevel`
- `dbops/graph_guards.py:72` (see A4)

### A7. Scheduled-routine git (self-repo automation, `push_mode`-independent)
- `src/schedules/common.py:265` `git <args>` wrapper; `:280` `git add`, `:283` `git diff --cached --quiet`, `:285` `git commit`, `:293` `git push origin main`, `:295` `git pull --rebase origin main` (autofix/reflect routines commit straight to main)

### A8. NOT a git dependency (verified — no shellout)
- **Cockpit "gitlog" view is a PURE RENDER.** `juggle_cockpit_gitlog_screen.py` / `_lines.py` / `_meta.py` render the **task-DAG** to look like `git log --graph`; there is **no** `subprocess`/`git` anywhere in `juggle_cockpit_gitlog*.py` or `juggle_cockpit_graph*.py` (grep confirmed NONE). `log_graph` in the sketched interface has **no current git call site** — the cockpit does not depend on git log. (`gitlog_screen.py:4` calls the pure `juggle_cockpit_gitlog_lines` core.)

**Count:** ~40 raw-git invocation sites across 8 modules (excludes the 9 already inside `vcs.py`). Heaviest concentration: `juggle_cmd_integrate.py` (~22 calls) and `dbops/graph_guards.py` + `juggle_integrate_mergedsha.py` (the verified gate).

---

## B. Existing dep-graph data model (degree queries)

**Confirmed: degree queries are trivial and cheap. No surprises for task-level;
one nuance at topic-level.**

- **Authoritative edge table is `node_edges`, NOT `graph_edges`.** The legacy
  `graph_edges` table (`schema_graph.py:27`) was **retired by Migration 55**
  (`db_graph_edges.py:3-6`). Anything designing against `graph_edges` is stale.
- Unified `nodes` table holds both `kind='topic'` and `kind='task'` rows;
  `node_edges` rows carry `kind='dep'` (dependency) or `kind='dispatch'`
  (task↔thread binding). `--deps` edges → `kind='dep'` rows.
- **Edges are one-directional** (`node_id` depends_on `depends_on_id`), read both ways:
  - in-deps: `get_deps(db, task_id)` — `db_graph_edges.py:25` (`WHERE node_id=? AND kind='dep'`)
  - out-deps (dependents): `get_dependents(db, task_id)` — `db_graph_edges.py:35` (`WHERE depends_on_id=? AND kind='dep'`)
  - → **in-degree = `len(get_deps(...))`, out-degree = `len(get_dependents(...))`.** Both are single indexed SELECTs. The H3 degree check is free.
- Edge writes: `replace_edges` (`db_graph_edges.py:11`) does `DELETE ... kind='dep'` then `INSERT OR IGNORE`. **No explicit self-loop guard** — a self-edge would `INSERT OR IGNORE` fine (A→A); worth a guard if degree logic assumes acyclic. **No uniqueness/DAG-acyclicity enforcement at the DB layer** either.
- **Topic-level degree is DERIVED, not stored.** `derived_topic_deps(db, topic_id)` (`db_topics.py:227`) computes cross-topic edges by joining task edges whose endpoints have different `parent_id` (topic). So topic in-degree is a `DISTINCT parent_id` query, not an O(1) column. For H3-at-topic-granularity, degree = distinct-parent join (still cheap, but not a plain `len()`).

---

## C. Worktree lifecycle + verified gate (detailed)

### Creation (per topic-thread, on dispatch)
- Entry: `juggle_dispatch_core.py:159-205`. Only `coder`/`planner` roles get a worktree. Base repo chosen by `resolve_worktree_base(main_repo_override, agent.repo_path, thread.main_repo_path, pane_id)` (`juggle_repo_binding.py:105`), rejecting `~/.claude`/plugin-dir bad bases.
- Actual create: `_create_worktree` → `git worktree add -b cyc_<label> <root>/juggle-<basename>-<label>` (`juggle_cmd_agents_worktree.py:125`). **Base commit-ish = implicit HEAD of the source repo's main worktree** (see H2). Idempotent if the path already exists (`:120`). Symlinks `.venv` (`:132`), pre-registers Claude trust (`:141`).
- Persisted on the thread row: `worktree_path`, `worktree_branch`, `main_repo_path` (`juggle_dispatch_core.py:188-193`).
- **No worktree is created per task** — tasks in a topic reuse the topic thread's single worktree (H3).

### Integrate (per topic, at the tail) — exact order
See §A3 for the line-by-line command list. Summary of the state machine:
1. source-binding guard (`_assert_source_binding`) → refuse if mis-bound (`:103`)
2. acquire serialized per-repo lock (`:119`)
3. dirty-worktree gate → refuse, never auto-commit (`:145`)
4. abort any in-progress rebase (`:158-169`)
5. `fetch --prune` (`:173`)
6. resolve rebase target `origin/main→origin/master→main→master` (`:179`)
7. empty-branch guard: `rev-list --count`, refuse if 0 ahead (`:192-207`)
8. `rebase <target>` → on conflict, abort + file action item, preserve branch (`:224-244`)
9. run FULL test suite verbatim if `test_cmd` set and `push_mode != none` (`:252-256`) — **B2: a subsetting `test_cmd` is refused fail-loud**
10. sync local main to base (`merge --ff-only` if tracked-dirty else `reset --hard`) (`:342-363`)
11. `merge --ff-only <branch>` into local main (`:367`)
12. `push origin main:main` if `push_mode=="direct"` (`:375`)
13. **record `merged_sha`** = local-main tip, AFTER push (`_record_merged_sha`, `:389`) — guarded to only record if the sha is a real ancestor of canonical origin/main
14. remove worktree + delete branch (`:392-398`), clear thread worktree fields (`:402`)
15. self-repo only: restart watchdog+monitor (`:407`)

### The `verified` gate (standing rule: verified ⟺ merged to trunk, immutable)
- **Single source of truth:** `topic_is_merged(db, topic_id)` (`graph_guards.py:114`) → true IFF the topic has a recorded `merged_sha` (`nodes.merged_sha`, written only by `set_topic_merged_sha`, `db_topics.py:155`) **that `sha_is_ancestor` of `main`** (`graph_guards.py:80`, `git merge-base --is-ancestor`).
- Enforced at the transition: `_verified_allowed` → `topic_transition` refuses `→verified` when the gate is closed, keeping it "pre-verified" (`db_topics.py:51,76-79`).
- **Fail-CLOSED by design** (`branch_merged_to_main` docstring, `graph_guards.py:49-55`): a NULL `merged_sha`, a deleted branch ref, or an unreadable repo all → NOT verified. Three prior false-verified incidents (2026-06-16) drove this.
- **Design consequence for H2/H3:** the proposed `integrated-unlanded` state must sit **below** `verified` and must **not** set `merged_sha` (or must set it to a not-yet-ancestor value that the gate correctly rejects). The gate already does the right thing — an unlanded stacked commit is not an ancestor of trunk, so it stays pre-verified until a **land-poller** re-checks ancestry and the watchdog promotes it. The gate needs **no change**; a poller that re-runs `topic_is_merged` on a schedule is the missing piece.

---

## D. Sapling / EdenFS mechanics (web research — flagged uncertainty)

> **Confidence: MEDIUM.** Open-source Sapling behavior is documented; the
> **Meta-internal fbsource land model is org-specific** and not fully public.
> Treat the land-queue specifics as "likely, verify against the actual target
> repo's tooling."

### D1. No `git worktree` equivalent — this is the root-cause confirmation
- Sapling deliberately does **not** implement `git worktree add`. Meta's position: worktrees don't scale to monorepo size; multiple checkouts are served by **EdenFS virtual clones** (`eden clone` → virtual working copy with fast `goto`) instead. [Sapling git support modes], [HN discussion].
- An fbsource checkout is a **symlink to an EdenFS+Sapling working copy with NO `.git`**, so `git worktree add` hard-fails — **this is the confirmed root cause** the task cites. A `SaplingBackend.create_worktree` cannot shell `git`; the isolation primitive is a **separate `sl`/EdenFS checkout** (or working within a single checkout via stack navigation), not an in-repo worktree.
- ezyang's "Parallel Agents ❤️ Sapling" (2026-03) confirms the parallel-agent workflow uses **multiple worktrees/checkouts** at different stack positions, and surfaces two stack-coordination commands: **`sl follow`** (move a checkout from a stale commit to its current successor) and **`sl adopt`** (rebase old children onto a new commit). Exact isolation mechanism (clone vs EdenFS redirect) is **not spelled out** in the post.

### D2. Landing: async, org-specific
- **Open-source GitHub mode:** land via PR submission (`sl pr submit` / ghstack-style stacked PRs); merging to trunk happens on the GitHub side. Roughly synchronous-ish per-PR but stack-aware. (Docs reference a separate "Using Sapling with GitHub" page not fully captured.)
- **Meta-internal fbsource:** landing goes through an **async land queue / land-bot** (Landcastle-style) — you submit a diff, it lands later, trunk advances out-of-band. This matches the task's "landing is slow/async" premise. **Org-specific; not publicly documented in detail — verify against the target repo's actual `sl` land command.**

### D3. Restack-on-land: **largely AUTOMATIC via mutation tracking** (key design fact)
- Sapling records every `amend`/`rebase`/`fold`/`split` as a **mutation** (its obsolescence-marker equivalent). [Visibility and mutation].
- **Crucial:** when the bottom of your stack lands on trunk (even rebased/squashed by CI), Sapling's mutation records let it recognize that "local commit #1 became commit X in main" and **automatically rebase commits #2–5 onto the new trunk head** — you don't hand-compute the restack. [FB engineering post; search synthesis]. After amending a mid-stack commit, `sl` **auto-restacks descendants** too (or `sl restack` if you deferred conflict resolution).
- **Nuance / not fully certain:** whether this is fully transparent on `sl pull` or needs an explicit `sl restack`/`sl pull --rebase`/`sl next --rebase` depends on config and whether conflicts arise. Content-addressing + mutation tracking make the *identity* mapping automatic; **conflict resolution is still manual**. For the **strict dependency chain** case H3 collapses into one worktree/checkout, restacking is moot within the chain (it was never parallel) — restack only matters at the chain **tail** landing and any **fan-out siblings** stacked on the same base.

**Design-relevant takeaway for H2/H3:** on Sapling the "stack-relative base ref" is the *native* model — a dependent task is simply a commit stacked on its dependency, and Sapling auto-restacks onto trunk when the dependency lands. So the Sapling backend's `integrate`/land is closer to "submit the stack + let mutation tracking restack," whereas the Git backend's equivalent (branch off dep tip, rebase onto new trunk head after dep lands) must be **explicit**. The `integrated-unlanded` state maps naturally onto "a Sapling commit that's stacked and tests-green but whose ancestor hasn't landed."

---

## Sources
- [Sapling — Git support modes](https://sapling-scm.com/docs/git/git_support_modes/)
- [Sapling — Stacks of commits](https://sapling-scm.com/docs/overview/stacks/)
- [Sapling — Visibility and mutation (internals)](https://sapling-scm.com/docs/dev/internals/visibility-and-mutation/)
- [Sapling — rebase command](https://sapling-scm.com/docs/commands/rebase/)
- [Meta Engineering — Sapling: source control that's scalable](https://engineering.fb.com/2022/11/15/open-source/sapling-source-control-scalable/)
- [ezyang — Parallel Agents ❤️ Sapling (2026-03)](https://blog.ezyang.com/2026/03/parallel-agents-heart-sapling/)
- [HN — Sapling discussion](https://news.ycombinator.com/item?id=33612410)
- [facebook/sapling issue #284 — landing with a sapling stack](https://github.com/facebook/sapling/issues/284)
- Retrieved 2026-07-02.
