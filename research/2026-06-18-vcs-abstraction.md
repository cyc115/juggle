---
topic: "VCS abstraction layer — juggle git audit + multi-VCS proposal"
date: 2026-06-20
tags: [research, vcs, git, hg, sapling]
---

# VCS Abstraction Research

> **Generated:** 2026-06-20 | Codebase: juggle @ `8813346`

## Executive Summary

Juggle has ~55 direct `git` subprocess call-sites across 10 source modules, with a thin partial abstraction already started in `src/vcs.py` (`VCS` Protocol with 3 methods: `head`, `is_dirty`, `make_safety_branch`). The codebase is `git`-coupled in three distinct tiers: (1) agent workspace isolation via `git worktree` — the deepest coupling and biggest design question; (2) the integrate pipeline (`juggle_cmd_integrate.py`) with 25+ calls spanning fetch/rebase/ff-merge/push/cleanup; and (3) schedule routines using a `git_run()` wrapper in `schedules/common.py`. The parallel-coder model REQUIRES isolated working directories; `git worktree` is the mechanism but `hg share` / Sapling native worktrees are valid analogs. The recommended abstraction is a **Python ABC `VcsBackend` with ~22 methods**, extending the existing `vcs.py` Protocol and covering all call-sites. Migration is ~40 mechanical substitutions + ~5 semantic re-expressions.

---

## PART A — Exhaustive Call-Site Inventory

### G1 — Repo identity / root

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `juggle_repo_binding.py` | 39–44 | `git -C cwd rev-parse --show-toplevel` | Walks up to find the git root; fallback when `canonical_repo_path()` fails |
| `juggle_cmd_graph.py` | 56–60 | `git -C cwd rev-parse --show-toplevel` | `_git_root()` — determines PR-mode refusal target repo |
| `vcs.py` | 83–85 | `(p / ".git").exists()` + `git rev-parse --is-inside-work-tree` | `detect()` — VCS type detection; `.git` dir existence check is a path assumption |
| `vcs.py` | 87 | `(p / ".hg").exists()` + `hg root` | Already has hg path check — partial hg detect |

**Semantics relied on:** POSIX path to the repo root. Git resolves worktree paths to their root correctly. Must work from inside a worktree.

---

### G2 — Worktree lifecycle

**Modules:** `juggle_cmd_agents_worktree.py`, `juggle_repo_binding.py`, `juggle_watchdog_singleton.py`, `juggle_cmd_integrate.py`

| File | Line(s) | Call | Semantics relied on |
|------|---------|------|---------------------|
| `juggle_cmd_agents_worktree.py` | 103–113 | `git -C repo worktree list --porcelain` | `_main_worktree_root()` — finds main worktree; first `worktree` line is always the primary; prevents nested-dispatch compound naming |
| `juggle_cmd_agents_worktree.py` | 134–139 | `git -C repo worktree add -b branch path` | `_create_worktree()` — creates isolated checkout + new branch `cyc_<thread>` atomically |
| `juggle_cmd_agents_worktree.py` | 73–75 | `git -C main worktree remove path` + `git -C main branch -d branch` | `_finalize_worktree()` — legacy cleanup path (pre-integrate path) |
| `juggle_repo_binding.py` | 79–88 | `git -C repo worktree list --porcelain` | `main_worktree_of()` — resolves any repo path (worktree or main) to the primary worktree path; used in mis-binding guard |
| `juggle_watchdog_singleton.py` | 154–161 | `git -C base worktree list --porcelain` | `canonical_repo_path()` — ensures watchdog starts from main checkout not a `cyc_*` worktree copy |
| `juggle_cmd_integrate.py` | 244 | `git -C main worktree remove --force path` | Idempotency cleanup (already-merged shortcut path) |
| `juggle_cmd_integrate.py` | 384 | `git -C main worktree remove --force path` | PR-mode: remove worktree but leave branch for PR |
| `juggle_cmd_integrate.py` | 436 | `git -C main worktree remove --force path` | Direct-mode: cleanup after ff-merge |
| `juggle_cmd_integrate.py` | 248 | `git -C main branch -D branch` | Force-delete on idempotency shortcut |
| `juggle_cmd_integrate.py` | 440 | `git -C main branch -d branch` | Normal delete post-merge |

**Semantics relied on:**
- `worktree list --porcelain`: first entry is ALWAYS the main worktree (guaranteed by git spec). Other worktrees follow.
- `worktree add -b branch path`: creates new branch + worktree atomically. Branch and worktree are 1:1.
- Worktrees share the same object store as the primary repo.
- Worktrees have their own working directory but branch references are global to the repo.
- `worktree remove` refuses if there are uncommitted changes (without `--force`).

---

### G3 — Branch: create / checkout / current-branch / delete

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `vcs.py` | 52–55 | `git branch name sha` + `git switch name` | `GitVCS.make_safety_branch()` — creates named branch at sha then checks it out |
| `juggle_cmd_integrate.py` | 356–358 | `git -C main symbolic-ref --short HEAD` | Gets current branch name in main worktree; used to validate we're on expected branch before merge |
| `schedules/autofix.py` | 687 | `git checkout -b branch` (via `git_run`) | Creates + checks out a new fix branch |
| `schedules/autofix.py` | 780 | `git branch -D branch` (via `git_run`) | Force-deletes branch on push failure |
| `schedules/autofix.py` | 781, 794 | `git checkout main` (via `git_run`) | Returns to main after branch work |

**Semantics relied on:** `symbolic-ref --short HEAD` returns the branch name (not detached SHA). Branch names are `cyc_<thread>` for coder tasks. `checkout -b` creates and switches in one step.

---

### G4 — Status / diff

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `vcs.py` | 48–50 | `git status --porcelain` | `GitVCS.is_dirty()` — non-empty output = dirty (staged or unstaged) |
| `juggle_cmd_integrate.py` | 262 | `git -C worktree diff --name-only --diff-filter=U` | List conflict files after failed rebase |
| `juggle_cmd_integrate.py` | 297–301 | `git -C worktree diff --name-only <ref>...HEAD` | Changed files in this branch for test scoping (3-dot = symmetric diff) |
| `juggle_cmd_integrate.py` | 404, 408 | `git -C main checkout -- graphify-out/` + `git -C main clean -fd -- graphify-out/` | Discard tracked and untracked changes to `graphify-out/` before ff-merge |
| `juggle_integrate_verify.py` | 70–72 | `git -C worktree diff --stat <ref>..HEAD` | Capture diffstat of rebased branch for task hydration (2-dot = range) |
| `schedules/common.py` | 278 | `git diff --cached --quiet` | Detect if there's anything staged to commit |

**Semantics relied on:**
- `--porcelain` is stable/parseable format.
- `--diff-filter=U` selects only unmerged (conflict) files.
- 3-dot `A...B` (symmetric) vs 2-dot `A..B` (range) are distinct and intentional.

---

### G5 — Commit: add, commit, rev-parse HEAD

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `vcs.py` | 45–46 | `git rev-parse HEAD` | `GitVCS.head()` — current HEAD sha for provenance recording |
| `juggle_cmd_integrate.py` | 50–56 | `git -C repo rev-parse ref` | Resolve branch name or ref to a sha (before recording as `merged_sha`) |
| `juggle_cmd_integrate.py` | 59–63 | `git -C repo cat-file -e sha` | Guard: verify object actually exists in object store before recording |
| `schedules/common.py` | 277 | `git add -- paths` or `git add -A` | Stage files for commit |
| `schedules/common.py` | 278 | `git diff --cached --quiet` | No-op detection before `git commit` |
| `schedules/common.py` | 282 | `git commit -m message` | Create commit |
| `schedules/autofix.py` | 75, 159, 228, 322, 369, 444, 473 | `git add + git commit` (via `git_run`) | Multiple per-fix-task commit points |

**Semantics relied on:** `rev-parse HEAD` returns the full 40-char SHA. `cat-file -e` exits 0 only when the object exists. `add -A` stages all changes including untracked.

---

### G6 — Sync: fetch, pull, push, rebase, merge

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `juggle_repo_binding.py` | 57–59 | `git -C repo fetch origin branch` | Targeted fetch for `canonical_main_ref()` — fetches both main/master before resolving `origin/main` |
| `juggle_cmd_integrate.py` | 184–187 | `git -C main fetch --prune` | Fetch all remotes + prune dead refs; non-fatal (repos without remotes) |
| `juggle_cmd_integrate.py` | 257 | `git -C worktree rebase rebase_onto` | Rebase branch onto `origin/main` (or local main); rewrites history |
| `juggle_cmd_integrate.py` | 267 | `git -C worktree rebase --abort` | Abort on conflict; restores pre-rebase state |
| `juggle_cmd_integrate.py` | 375–378 | `git -C worktree push origin branch:branch --force-with-lease` | PR mode: push rebased branch to origin |
| `juggle_cmd_integrate.py` | 414 | `git -C main merge --ff-only branch` | Direct mode: fast-forward merge; fails if branch diverged from main |
| `juggle_cmd_integrate.py` | 427–429 | `git -C main push origin main:main` | Direct mode: push merged main to origin |
| `juggle_cmd_agents_worktree.py` | 62–63 | `git -C main merge --ff-only branch` | Legacy finalize path (pre-integrate migration) |
| `schedules/common.py` | 292 | `git push origin main` | Push juggle repo main branch |
| `schedules/common.py` | 295–296 | `git pull --rebase origin main` + retry | Pull + rebase on push rejection |

**Semantics relied on:**
- `rebase` rewrites commits onto new base; requires no uncommitted changes.
- `--ff-only` merge ONLY succeeds when the merge would be a fast-forward (branch is ahead of main on a linear path). This is the key invariant: the integrate pipeline guarantees linear history.
- `--force-with-lease` (PR mode) is safer than `--force`: refuses if remote was updated since last fetch.
- `--prune` on fetch removes remote tracking refs for deleted remote branches.

---

### G7 — History / query: log, rev-parse, merge-base, ancestor checks

| File | Line | Call | Semantics relied on |
|------|------|------|---------------------|
| `juggle_repo_binding.py` | 62–65 | `git -C repo rev-parse --verify candidate` | Verify that `origin/main`, `origin/master`, `main`, `master` refs exist; first match wins |
| `juggle_cmd_integrate.py` | 192–196 | `git -C main rev-parse --verify candidate` | Same — rebase target resolution |
| `juggle_cmd_integrate.py` | 202–206 | `git -C main rev-list --count ref..branch` | Count commits branch is ahead of rebase target; `0` → already merged shortcut |
| `juggle_cmd_integrate.py` | 80–83 | `git -C repo merge-base --is-ancestor sha canonical` | G1 guard: sha must be ancestor of canonical main before recording as `merged_sha` |
| `juggle_cmd_integrate.py` | 227–231 | `git -C main merge-base --is-ancestor branch canonical` | Shortcut ancestor check: confirms 0-ahead-count is not a phantom |
| `juggle_cmd_integrate.py` | 170–176 | `git -C worktree rev-parse --git-dir` + path check | Detect in-progress rebase (`rebase-merge`/`rebase-apply` directories in git dir) |
| `dbops/graph_guards.py` | 60 | `git -C repo rev-parse --verify branch` | G1 gate: confirm branch ref still exists |
| `dbops/graph_guards.py` | 62 | `git -C repo merge-base --is-ancestor branch main` | `branch_merged_to_main()` — branch ancestry check (now secondary to `merged_sha`) |
| `dbops/graph_guards.py` | 71–75 | `git -C repo rev-parse --verify branch` | `resolve_branch_sha()` — resolve branch to sha |
| `dbops/graph_guards.py` | 88 | `git -C repo merge-base --is-ancestor sha main` | `sha_is_ancestor()` — THE authoritative verified-gate; checks recorded `merged_sha` is ancestor of main |
| `schedules/autofix.py` | 413 | `git log --since=7 days ago --oneline --no-merges` | FX-6: get recent commits for CHANGELOG generation |

**Key invariant:** `verified ⟺ merged_sha is ancestor of main`. `sha_is_ancestor()` in `graph_guards.py:88` is the single source of truth for this. `branch_merged_to_main()` is a secondary helper.

---

### G8 — Hooks / ignore

No `.gitignore` manipulation or commit hook invocation in juggle's Python code. Agents interact with `.gitignore` via Bash calls (not tracked by Juggle). `juggle_hooks_tooluse.py:41` has a descriptive string `"git write operation"` but is not a git call.

---

## PART B — Non-1:1 VCS Semantics (the hard parts)

### B1 — Worktrees (BIGGEST risk)

**Git:** `git worktree add -b branch path` creates an isolated working directory that shares the same object store as the primary repo. The branch ref is scoped to the whole repo. `worktree list --porcelain` first entry is always the primary worktree.

**hg / Sapling:**
- **`hg share`**: Creates a secondary working directory sharing the same `.hg` store. Bookmarks/branches are shared. Close analog — but `hg share` does NOT create a new branch atomically; you `hg share` then `hg update -r bookmark`.
- **Sapling (sl):** Native `sl worktree add` command added in 2023 — close to `git worktree` semantics. This is the closest hg-family analog.
- **Meta's hg:** Uses Eden FUSE virtual filesystem for COW working directory snapshotting. The isolation is at the FS layer, not the VCS layer. `hg checkout` is fast (O(metadata), not O(files)). Meta's internal `jf` (Jujutsu-style) or `sl` are likely the right commands.

**Impact on Juggle:** The parallel-coder model requires per-task isolated working directories. This IS required — it's architectural, not optional. The abstraction must expose `workspace_create(repo, path, branch)` and `workspace_remove(repo, path)` with backend-specific implementations.

**Worktree→branch coupling:** In git, `worktree add -b branch path` creates the branch atomically. In hg, workspace creation and bookmark creation are separate steps. The abstraction can hide this but implementations differ.

---

### B2 — Branch model: `cyc_<thread>` naming

**Git:** Lightweight branches are pointers to commits. `cyc_<thread>` branch per task. Branch delete after merge is idempotent.

**hg:** Named branches (heavyweight, stored in commit metadata — BAD) vs bookmarks (lightweight, git-branch analog). Juggle should use bookmarks. Sapling uses bookmarks by default.

**hg `symbolic-ref` analog:** `hg branch` returns the named branch (if using named branches), `hg bookmark` shows active bookmark. The equivalent of `git symbolic-ref --short HEAD` for "what branch am I on" is `hg bookmark --active` in bookmark-based hg.

**Impact on Juggle:** `_create_worktree()` creates branch `cyc_<thread>` — must become `create_bookmark(name)` for hg. The `worktree_branch` DB column stores this name; it's VCS-agnostic as a string.

---

### B3 — Rebase + ff-merge (the land flow)

**Git (current):**
1. `git fetch --prune`
2. `git rebase origin/main` (in worktree)
3. `git merge --ff-only branch` (in main)

**hg:**
- `hg pull && hg rebase -d main -r tip` (with evolve extension, unstable commits OK)
- No `--ff-only` concept; `hg merge` is always a real merge. Instead: `hg update -r bookmark` then check that `hg parents` shows a fast-forward.
- Alternative: `hg graft` (cherry-pick analog)

**Sapling:**
- `sl rebase -d main -r 'bookmark(.)'`
- `sl merge --ff-only` (Sapling added --ff-only, close to git)

**Meta's land flow:** Typically uses `jf submit` + automated `@land` CI rather than local ff-merge. The concept of "land to main" is a push to a review/land queue, not a local merge. This is the DEEPEST semantic divergence: Juggle's integrate pipeline assumes a local ff-merge + push model, but Meta's repos use a centralized land service.

**Impact on Juggle:** `rebase(repo, onto) -> (ok, conflict_detail)` and `merge_ff(repo, branch) -> (ok, msg)` are the two key methods. For Meta repos, `merge_ff` may need to be `land(branch) -> sha` that calls the land service.

**Rebase-in-progress detection:** Git detects via `Path(git_dir, "rebase-merge").exists()`. hg: `hg resolve -l` returning non-empty OR checking for `.hg/merge/state`. Must be a backend method.

---

### B4 — Ancestor / merged checks (G1 gate)

**Git:** `git merge-base --is-ancestor sha main` → exit 0 if true.

**hg:** `hg log -r "sha and ancestors(main)" --template "x"` → non-empty if true. Or: `hg debugancestor sha main` → prints the ancestor (sha if it's ancestor of main). Or: `hg log -r "sha % main" --template "x"` (revset: sha that are ancestors of main).

**Sapling:** `sl log -r "sha and ancestors(main)"` — same revset syntax.

**Impact on Juggle:** `sha_is_ancestor(repo, sha, main)` in `graph_guards.py:88` is the G1 gate. This MUST be correct — false positives mark tasks verified without actually being merged. The hg revset approach is semantically equivalent but syntactically different.

---

### B5 — Remote model: `origin/main`

**Git:** After `git fetch`, `origin/main` is a local ref. `rev-parse --verify origin/main` either succeeds (ref exists) or fails.

**hg:** No local remote-tracking refs. `hg pull` updates the repo; `hg log -r "remote()"` or `hg log -r "bookmark('remote/main')"` (for hg remote bookmarks). The concept of `origin/main` must be expressed as `remote_bookmark("main")` or equivalent.

**hg candidate resolution:** Instead of iterating `["origin/main", "origin/master", "main", "master"]`, hg would iterate `["remote/main", "remote/master", "@", "main"]` (using remote bookmarks or named branches).

**Impact on Juggle:** `resolve_main_ref(repo) -> str | None` abstracts this. The return value may be `"origin/main"` (git), `"remote/main"` (hg), or a sha (for bare resolve).

---

## PART C — VCS Abstraction Proposal

### Minimum `VcsBackend` interface

Derived from exhaustive Part A inventory. All methods return `None`/`False`/`""` on failure (best-effort, matching `vcs.py`'s current pattern).

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class VcsBackend(Protocol):
    # ── Identity ─────────────────────────────────────────────────────────────
    def detect(self, path: str) -> bool:
        """True iff path is inside a repo of this VCS type."""
    
    def repo_root(self, path: str) -> str | None:
        """Repo root (git: --show-toplevel; hg: hg root)."""

    # ── State ─────────────────────────────────────────────────────────────────
    def head(self, path: str) -> str | None:
        """Current HEAD sha (40-char)."""

    def is_dirty(self, path: str) -> bool:
        """True iff working tree has uncommitted changes (staged or unstaged)."""

    def uncommitted_files(self, path: str) -> list[str]:
        """List of modified/added/removed files (for conflict detection)."""

    # ── Current branch ────────────────────────────────────────────────────────
    def current_branch(self, path: str) -> str | None:
        """Active branch/bookmark name, or None if detached."""

    # ── Ref resolution ────────────────────────────────────────────────────────
    def ref_exists(self, repo: str, ref: str) -> bool:
        """True iff ref (branch/bookmark/remote-ref) exists."""

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        """Resolve ref to a sha."""

    def resolve_main_ref(self, repo: str) -> str | None:
        """Best canonical main ref after fetching (e.g. 'origin/main').
        Tries: origin/main, origin/master, main, master (git);
               remote/main, @, main (hg)."""

    # ── Ancestry / history ────────────────────────────────────────────────────
    def is_ancestor(self, repo: str, sha: str, target_ref: str) -> bool:
        """True iff sha is an ancestor of target_ref. THE G1 gate."""

    def commits_ahead(self, repo: str, base: str, tip: str) -> int:
        """Number of commits in tip but not in base (git: rev-list --count)."""

    def object_exists(self, repo: str, sha: str) -> bool:
        """True iff sha object exists in repo (git: cat-file -e)."""

    def log_oneline(self, repo: str, since: str) -> str:
        """One-line log since a date string, no merges."""

    # ── Workspace (worktree / share) ─────────────────────────────────────────
    def workspace_list_primary(self, repo: str) -> str:
        """Path of the primary (main) workspace for this repo.
        From any worktree/share, returns the main checkout path."""

    def workspace_create(self, repo: str, path: str, branch: str) -> tuple[bool, str]:
        """Create isolated workspace at path on new branch. Returns (ok, msg)."""

    def workspace_remove(self, repo: str, path: str, force: bool = False) -> tuple[bool, str]:
        """Remove isolated workspace. Returns (ok, msg)."""

    def workspace_rebase_in_progress(self, path: str) -> bool:
        """True iff a rebase/merge is in progress in this workspace."""

    # ── Branch ────────────────────────────────────────────────────────────────
    def branch_create(self, repo: str, name: str, at_sha: str) -> bool:
        """Create branch/bookmark at sha (does NOT checkout)."""

    def branch_delete(self, repo: str, name: str, force: bool = False) -> bool:
        """Delete local branch/bookmark."""

    def checkout(self, repo: str, ref: str) -> bool:
        """Checkout branch/bookmark in repo."""

    def make_safety_branch(self, path: str, sha: str, name: str) -> bool:
        """Create branch at sha and check it out (for restore operations)."""

    # ── Sync ─────────────────────────────────────────────────────────────────
    def fetch(self, repo: str) -> bool:
        """Fetch from default remote (non-fatal on no-remote repos)."""

    def rebase(self, repo: str, onto: str) -> tuple[bool, str]:
        """Rebase current branch onto onto. Returns (ok, conflict_detail)."""

    def rebase_abort(self, repo: str) -> None:
        """Abort in-progress rebase."""

    def merge_ff(self, repo: str, branch: str) -> tuple[bool, str]:
        """Fast-forward merge branch into HEAD. Returns (ok, msg)."""

    def push(self, repo: str, branch: str, remote: str = "origin",
             force_with_lease: bool = False) -> tuple[bool, str]:
        """Push branch to remote. Returns (ok, msg)."""

    def pull_rebase(self, repo: str, remote: str = "origin",
                    branch: str = "main") -> bool:
        """Pull + rebase (on push rejection)."""

    # ── Staging / commit ─────────────────────────────────────────────────────
    def add(self, repo: str, paths: list[str] | None = None) -> None:
        """Stage paths (or all changes if None)."""

    def has_staged_changes(self, repo: str) -> bool:
        """True iff there are staged changes ready to commit."""

    def commit(self, repo: str, message: str) -> bool:
        """Create commit. Returns False if nothing to commit."""

    # ── Diff ─────────────────────────────────────────────────────────────────
    def diff_stat(self, repo: str, base: str, tip: str = "HEAD") -> str:
        """Diffstat of range base..tip (2-dot). For task hydration."""

    def changed_files(self, repo: str, base: str, tip: str = "HEAD",
                      symmetric: bool = True) -> list[str]:
        """Files changed in range. symmetric=True → 3-dot (changed vs base)."""

    def conflict_files(self, repo: str) -> list[str]:
        """Files with merge/rebase conflicts (unmerged state)."""

    # ── Discard ──────────────────────────────────────────────────────────────
    def discard_path(self, repo: str, path: str) -> None:
        """Discard tracked + untracked changes under path (pre-merge cleanup)."""
```

**Total: 29 methods** (vs 3 in current `vcs.py`). Maps to every distinct git operation found in Part A.

---

### Mapping: call-site → backend method

| Call-site module | Git calls today | Backend method(s) |
|------------------|----------------|-------------------|
| `juggle_repo_binding.py` | `rev-parse --show-toplevel`, `fetch origin branch`, `rev-parse --verify`, `worktree list` | `repo_root`, `fetch`, `resolve_main_ref`, `workspace_list_primary` |
| `juggle_cmd_agents_worktree.py` | `worktree list`, `worktree add -b`, `merge --ff-only`, `worktree remove`, `branch -d` | `workspace_list_primary`, `workspace_create`, `merge_ff`, `workspace_remove`, `branch_delete` |
| `juggle_cmd_integrate.py` | 25 calls (fetch, rebase, rev-parse, rev-list, merge-base, push, merge --ff-only, symbolic-ref, cat-file, diff, clean, checkout, worktree remove, branch -d) | `fetch`, `rebase`, `rebase_abort`, `workspace_rebase_in_progress`, `ref_exists`, `commits_ahead`, `is_ancestor`, `resolve_main_ref`, `current_branch`, `push`, `merge_ff`, `object_exists`, `discard_path`, `workspace_remove`, `branch_delete` |
| `juggle_integrate_verify.py` | `diff --stat` | `diff_stat` |
| `dbops/graph_guards.py` | `rev-parse --verify`, `merge-base --is-ancestor` (×3) | `ref_exists`, `resolve_ref`, `is_ancestor` |
| `juggle_watchdog_singleton.py` | `worktree list` | `workspace_list_primary` |
| `juggle_cmd_graph.py` | `rev-parse --show-toplevel` | `repo_root` |
| `vcs.py` | `rev-parse HEAD`, `status --porcelain`, `branch name sha`, `switch name` | already `head`, `is_dirty`, `make_safety_branch` (kept) |
| `schedules/common.py` (git_run/git_commit/git_push) | `add`, `diff --cached`, `commit`, `push`, `pull --rebase` | `add`, `has_staged_changes`, `commit`, `push`, `pull_rebase` |
| `schedules/autofix.py` | `diff --stat`, `add -A`, `commit -m`, `log --since`, `checkout -b`, `push -u`, `branch -D`, `checkout main` | `diff_stat`, `add`, `commit`, `log_oneline`, `workspace_create` (or just `checkout + branch`), `push`, `branch_delete`, `checkout` |

**Call-sites that become VCS-agnostic:** All 10 modules above.
**Call-sites that stay git-only:** None. Even `schedules/` references only JUGGLE_REPO which is always git (juggle IS a git repo), but abstracting schedules allows future flexibility.

---

### Git implementation notes (from Part A)

All methods map mechanically to the subprocess calls already in use. Key non-obvious git details to preserve:
- `workspace_rebase_in_progress`: check `Path(git_dir, "rebase-merge").exists() or Path(git_dir, "rebase-apply").exists()` (both directories)
- `resolve_main_ref`: must `fetch` first, then try `["origin/main", "origin/master", "main", "master"]` in order
- `commits_ahead`: `rev-list --count base..tip` (empty string output on error → default 1, not 0, to avoid false already-merged)
- `merge_ff` in main repo: must first `discard_path(graphify-out/)` to avoid false conflicts (2026-06-14 bug)
- `discard_path`: requires BOTH `checkout -- path` (tracked changes) AND `clean -fd -- path` (untracked files)

### hg/Sapling implementation sketch

| Method | hg command | Sapling (sl) |
|--------|-----------|--------------|
| `repo_root` | `hg root` | `sl root` |
| `head` | `hg id -i` (strip trailing `+`) | `sl id -i` |
| `is_dirty` | `hg status` non-empty | `sl status` non-empty |
| `current_branch` | `hg bookmark --active` | `sl bookmark --active` |
| `ref_exists` | `hg log -r name --template x` non-empty | same |
| `resolve_ref` | `hg log -r name --template {node}` | same |
| `is_ancestor` | `hg log -r "name and ancestors(main)" --template x` non-empty | same |
| `commits_ahead` | `hg log -r "base::tip - base" --template x \| wc -c` | same |
| `workspace_list_primary` | `hg root` (hg share stores primary path in `.hg/sharedpath`) | `sl root` or first `sl worktree list` entry |
| `workspace_create` | `hg share repo path && cd path && hg bookmark branch && hg update -r branch` | `sl worktree add -B branch path` |
| `workspace_remove` | `rm -rf path` (hg share dirs are just dirs) | `sl worktree remove path` |
| `workspace_rebase_in_progress` | `hg resolve -l` non-empty OR `.hg/merge/state2` exists | same |
| `fetch` | `hg pull` | `sl pull` |
| `rebase` | `hg rebase -d onto -r tip` (evolve ext) | `sl rebase -d onto -r .` |
| `rebase_abort` | `hg rebase --abort` | `sl rebase --abort` |
| `merge_ff` | `hg update -r branch` (ff semantics: only if branch is descendant of current) | `sl merge --ff-only branch` (native) |
| `push` | `hg push -B branch` | `sl push -B branch` |
| `branch_create` | `hg bookmark -r sha name` | `sl bookmark -r sha name` |
| `branch_delete` | `hg bookmark -d name` | `sl bookmark -d name` |
| `checkout` | `hg update -r name` | `sl update -r name` |
| `add` | `hg add [paths]` | `sl add [paths]` |
| `commit` | `hg commit -m msg` | `sl commit -m msg` |
| `diff_stat` | `hg diff --stat -r base::tip` | same |
| `changed_files` | `hg log -r "base::tip" -T "{files}\n"` or `hg diff --stat -r base -r tip` | same |
| `discard_path` | `hg revert path && hg purge path` | same |

**Meta-hg specifics:** Meta uses Mononoke (server-side) + Eden (client FUSE FS). The checkout model is fundamentally different — working directory is virtual. `sl` (Sapling) is Meta's open-source release of their internal `hg` fork. For Meta repos, `workspace_create` likely wraps `sl cloud workspace create` or `eden redirect add`. The land flow uses `sl land` or Phabricator/Sandcastle automation, not local ff-merge. This means `merge_ff` for Meta repos would call a land service API, not a local git command.

---

### Design options

#### Option 1 — Thin shell-command adapter (extend current `vcs.py`)

Extend the existing `VCS` Protocol in `vcs.py` to the full 29-method interface. `GitVCS` and `HgVCS` classes implement each method as a subprocess call. Detection via `detect()`. Dependency injection via `get_backend(detect(path))`.

**Pro:** Already started, zero new abstractions, no dependencies, matches juggle's "thin orchestrator" philosophy.  
**Con:** 29 × 3 backends = 87 method implementations. Deeply divergent semantics (land flow) are hidden but complex.  
**Migration cost:** Low — all call-sites do a mechanical swap.

#### Option 2 — Python ABC with backends ⭐ RECOMMENDED

```python
# src/vcs_backend.py
from abc import ABC, abstractmethod

class VcsBackend(ABC):
    @abstractmethod
    def repo_root(self, path: str) -> str | None: ...
    # ... all 29 methods
    
class GitBackend(VcsBackend): ...
class HgBackend(VcsBackend): ...
class SaplingBackend(HgBackend): ...  # inherits hg, overrides workspace + merge
```

`detect_backend(path: str) -> VcsBackend | None` replaces `detect()` + `get_backend()`. Config override via `JUGGLE_VCS=git|hg|sl` or per-repo `juggle.toml` `[vcs] type = "sl"`.

**Pro:** Enforced contract (`abstractmethod` catches missing impls at import), enables conformance test suite, `SaplingBackend` inherits `HgBackend` and overrides only divergent methods (worktrees, merge_ff).  
**Con:** More ceremony than Option 1. Same underlying complexity.  
**Migration cost:** Low — call-sites identical to Option 1.  
**Recommended because:** The conformance test suite is only possible with a formal class hierarchy; `@abstractmethod` catches missing backend methods at development time; `SaplingBackend(HgBackend)` cleanly models that Sapling is mostly hg.

#### Option 3 — External library

- **GitPython** ⭐ ~16k: High-level Python git interface. Does NOT cover hg/sapling. Adds a heavy dependency for coverage of only the git backend.
- **pygit2** ⭐ ~1.1k: libgit2 bindings. Excellent for git internals but same hg gap.
- **python-hglib**: Official Mercurial Python bindings. Only covers hg; no sapling support.

**Verdict:** External libraries don't help with the hard part (hg/sapling semantics) and add dependencies juggle deliberately avoids. Skip.

---

### Backend selection / DI

```python
# src/vcs_backend.py

def detect_backend(path: str) -> VcsBackend | None:
    """Select a backend by detecting the VCS at path, with config override."""
    override = os.environ.get("JUGGLE_VCS")
    if not override:
        from juggle_settings import get_nested
        override = get_nested("vcs", "type", None)  # per-repo config
    
    if override == "git" or (not override and _has_git(path)):
        return GitBackend()
    if override in ("hg", "sapling", "sl") or (not override and _has_hg(path)):
        vcs_type = override or "hg"
        return SaplingBackend() if vcs_type in ("sapling", "sl") else HgBackend()
    return None

def _has_git(path: str) -> bool:
    return (Path(path) / ".git").exists() or bool(_run(["git", "rev-parse", "--is-inside-work-tree"], path))

def _has_hg(path: str) -> bool:
    return (Path(path) / ".hg").exists() or (Path(path) / ".sl").exists()
```

Call-sites pass `repo_path` to `detect_backend()` once per invocation. Cache per process where performance matters (watchdog daemon).

---

### Migration / refactor size estimate

| Module | Direct git calls | Change type |
|--------|-----------------|-------------|
| `juggle_cmd_integrate.py` | 25 | Mechanical (swap subprocess for `backend.method()`) + 2 semantic (`merge_ff` graphify-out discard, `workspace_rebase_in_progress`) |
| `juggle_cmd_agents_worktree.py` | 5 | Mechanical |
| `juggle_repo_binding.py` | 4 | Mechanical |
| `schedules/common.py` | ~8 | Mechanical (git_run/git_commit/git_push → backend methods) |
| `schedules/autofix.py` | ~15 | Mechanical (via git_run already wrapped) |
| `dbops/graph_guards.py` | 4 | Mechanical |
| `juggle_watchdog_singleton.py` | 1 | Mechanical |
| `juggle_cmd_graph.py` | 1 | Mechanical |
| `vcs.py` | 6 | Already abstracted; merge into `vcs_backend.py` |
| `schedules/reflect.py`, `dogfood.py` | 2 each | Mechanical |
| **Total** | **~55** | **~50 mechanical, ~5 semantic** |

**Refactor phases:**
1. `vcs_backend.py` — ABC + GitBackend (all 29 methods), extend `vcs.py` impls
2. `dbops/graph_guards.py` — swap 4 git calls (lowest risk, most critical correctness)
3. `juggle_cmd_integrate.py` — swap 25 calls (highest call-density, most tested)
4. Remaining modules — mechanical sweep
5. `HgBackend` + `SaplingBackend` — implement incrementally against conformance suite

---

### Backend Conformance Test Suite

Abstract mixin class; same tests run against both git and hg backends. Each test sets up a minimal repo using the VCS CLI, runs a backend method, asserts the result.

```python
# tests/test_vcs_backend_conformance.py

import pytest
import subprocess
from pathlib import Path

class VcsConformanceTests:
    """Mix in: set self.backend + self.repo_path in setUp."""
    backend = None  # VcsBackend instance

    def init_repo(self, path: Path): ...
    def make_commit(self, path: Path, msg: str = "init"): ...
    def make_branch(self, path: Path, name: str): ...

    def test_repo_root(self): ...
    def test_head_sha_40chars(self): ...
    def test_is_dirty_clean(self): ...
    def test_is_dirty_with_modification(self): ...
    def test_uncommitted_files(self): ...
    def test_current_branch_name(self): ...
    def test_ref_exists_true(self): ...
    def test_ref_exists_false(self): ...
    def test_resolve_ref_sha_format(self): ...
    def test_is_ancestor_true(self): ...
    def test_is_ancestor_false(self): ...
    def test_commits_ahead_zero(self): ...
    def test_commits_ahead_nonzero(self): ...
    def test_object_exists_true(self): ...
    def test_object_exists_false(self): ...
    def test_workspace_create_and_list_primary(self): ...
    def test_workspace_remove(self): ...
    def test_workspace_rebase_in_progress_false(self): ...  # no rebase active
    def test_fetch_noop_no_remote(self): ...  # should not raise
    def test_diff_stat_nonempty(self): ...
    def test_changed_files(self): ...
    def test_conflict_files_empty(self): ...  # no conflict state
    def test_add_and_commit(self): ...
    def test_has_staged_changes(self): ...
    def test_merge_ff_success(self): ...
    def test_merge_ff_fail_diverged(self): ...  # must fail, not raise
    def test_push_noop_no_remote(self): ...

class TestGitBackend(VcsConformanceTests):
    backend = GitBackend()
    def init_repo(self, path): subprocess.run(["git", "init", str(path)], ...)
    # ... setup using git CLI

@pytest.mark.skipif(shutil.which("hg") is None, reason="hg not available")
class TestHgBackend(VcsConformanceTests):
    backend = HgBackend()
    def init_repo(self, path): subprocess.run(["hg", "init", str(path)], ...)

@pytest.mark.skipif(shutil.which("sl") is None, reason="sl/sapling not available")  
class TestSaplingBackend(VcsConformanceTests):
    backend = SaplingBackend()
    def init_repo(self, path): subprocess.run(["sl", "init", str(path)], ...)
```

**Deterministic CLI signals per operation (git):**
- `is_ancestor`: exit code 0 = true, non-zero = false (no stdout needed)
- `commits_ahead`: stdout is an integer string; `""` → parse error → treat as 1 (fail-safe)
- `merge_ff`: exit 0 = success; stderr contains human-readable failure reason
- `workspace_rebase_in_progress`: filesystem check, no subprocess needed

---

## Cross-References

- `src/vcs.py`: Current partial abstraction (3 methods). Starting point for Option 2.
- `src/juggle_cmd_integrate.py`: Densest git coupling; the core integration pipeline.
- `src/dbops/graph_guards.py`: The G1 verified-gate; `sha_is_ancestor()` is the single source of truth.
- `src/juggle_cmd_agents_worktree.py`: Worktree lifecycle; biggest semantic gap for hg.
- `src/juggle_repo_binding.py`: Canonical repo resolution; mis-binding guard.
- `docs/incidents/2026-06-13-autopilot-shared-db-corruption.md`: Motivates G1/G2 guards.

## Gaps / Open Questions

- [ ] **Meta-hg land flow**: Does juggle's integrate pipeline need to support a "push to land queue" flow instead of local ff-merge? If so, `merge_ff` for Meta repos becomes `land(branch) -> sha` calling an internal API.
- [ ] **Eden FS workspace creation**: `hg share` vs Eden virtual checkout — which does Meta's internal tooling use? This determines `workspace_create` impl for Meta backend.
- [ ] **Sapling worktree stability**: `sl worktree add` was added in 2023 — version minimum needed?
- [ ] **`schedules/`**: Schedule routines hardcode `JUGGLE_REPO` (juggle IS a git repo) — should they stay git-only or also abstract?
- [ ] **Test infra**: Conformance tests for `rebase` + `merge_ff` require 2-commit histories with divergence; confirm test setup complexity is acceptable.
- [ ] **`.sl` detection**: Sapling may use `.sl/` instead of `.hg/` — verify `detect()` covers both.

## Recommended Next Steps

1. Implement `src/vcs_backend.py` with `VcsBackend` ABC + `GitBackend` (all 29 methods), extending existing `vcs.py`.
2. Write conformance suite for `GitBackend` first; run against all 23 tests to green.
3. Port `dbops/graph_guards.py` (4 call-sites, most critical for correctness) as the first mechanical migration.
4. Port `juggle_cmd_integrate.py` (25 call-sites, most complex) as the main migration.
5. Implement `HgBackend` + `SaplingBackend` against conformance suite (skip hg tests if `hg` not installed).
6. Add `juggle_settings` entry `[vcs] type = "git|hg|sl"` for per-repo override.
