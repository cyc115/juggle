"""vcs_git — GitVCS: the git implementation of the widened VCS Protocol.

Owns: every git shellout the Protocol methods need. Each method body is the
EXACT current shellout sequence relocated from its call site (facts §A) —
behavior-identical, not a new recipe. Call sites are rewired to route through
these methods in later topics; this module only defines the mechanics.

Must not own: juggle naming conventions (``cyc_<label>``, ``juggle-<basename>-
<label>``) — callers compute branch/path names and pass them in as ``name``/
``root``. Must not own orchestrator policy (locks, dirty gates, test runs,
action-item filing) — that stays in the calling pipeline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from vcs_types import (
    Capabilities,
    LandStatus,
    Rev,
    SubmitMode,
    SubmitResult,
    UpdateResult,
    WorkspaceResult,
    _run,
)


class GitVCS:
    """git backend for the VCS Protocol."""

    name = "git"
    capabilities = Capabilities(async_land=False, auto_restack=False)

    # ── identity & detection ────────────────────────────────────────────────

    def repo_root(self, path: str) -> str | None:
        if not path:
            return None
        return _run(["git", "-C", path, "rev-parse", "--show-toplevel"], path)

    def primary_root(self, repo: str) -> str:
        """First entry of ``git worktree list --porcelain`` is always the main
        worktree — stable even when called from a linked worktree."""
        out = _run(["git", "-C", repo, "worktree", "list", "--porcelain"], repo)
        if out:
            for line in out.splitlines():
                if line.startswith("worktree "):
                    return str(Path(line[len("worktree "):].strip()).resolve())
        return repo

    # ── history facts ────────────────────────────────────────────────────────

    def refresh(self, repo: str) -> None:
        _run(["git", "-C", repo, "fetch", "--prune"], repo)

    def trunk(self, repo: str) -> Rev | None:
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if _run(["git", "-C", repo, "rev-parse", "--verify", candidate], repo) is not None:
                return candidate
        return None

    def resolve(self, repo: str, ref: str | None = None) -> Rev | None:
        if ref is None:
            return _run(["git", "-C", repo, "rev-parse", "HEAD"], repo)
        return _run(["git", "-C", repo, "rev-parse", "--verify", ref], repo)

    def is_ancestor(self, repo: str, rev: str, of: str) -> bool:
        if not repo or not rev or not of or not Path(repo).exists():
            return False
        return _run(["git", "-C", repo, "merge-base", "--is-ancestor", rev, of], repo) is not None

    def commits_landed(self, repo: str, rev: str, of: str) -> bool:
        """True iff every commit in ``of..rev`` has a content-equivalent already
        on ``of`` — the rebase/cherry-pick/squash landing that ancestry
        (``is_ancestor``) is blind to (2026-07-03 integrate-wedge amendment).

        ``git cherry <upstream> <head>`` prefixes each commit with ``-`` when it
        has an equivalent upstream (diff-based, whitespace/line-number-
        insensitive) and ``+`` when it does not. LANDED = at least one commit
        ahead AND every one prefixed ``-``. An empty output (branch is an
        ancestor of ``of``, or no commits ahead) is NOT a content landing — the
        ancestry tier owns that case. Fail-closed on any git error / bad repo."""
        if not repo or not rev or not of or not Path(repo).exists():
            return False
        out = _run(["git", "-C", repo, "cherry", of, rev], repo)
        if out is None:
            return False
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            return False
        return all(ln.startswith("-") for ln in lines)

    # ── workspace lifecycle ──────────────────────────────────────────────────

    def create_workspace(
        self, repo: str, name: str, root: str, *, base: Rev | None = None
    ) -> WorkspaceResult:
        if Path(root).exists():
            return WorkspaceResult(
                ok=True, path=root, branch=name,
                detail=f"Worktree already exists: {root}",
            )
        args = ["git", "-C", repo, "worktree", "add", "-b", name, root]
        if base:
            args.append(base)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as e:
            return WorkspaceResult(ok=False, path="", branch="", detail=str(e))
        if r.returncode != 0:
            return WorkspaceResult(ok=False, path="", branch="", detail=r.stderr.strip())
        return WorkspaceResult(
            ok=True, path=root, branch=name,
            detail=f"Worktree created: {root} on branch {name}",
        )

    def remove_workspace(self, repo: str, ws: str) -> bool:
        """Full teardown including branch cleanup — branch bookkeeping is
        backend-private, so the branch name is resolved from ``ws`` itself
        rather than threaded in by the caller."""
        branch = _run(["git", "-C", ws, "symbolic-ref", "--short", "HEAD"], ws)
        ok = _run(["git", "-C", repo, "worktree", "remove", "--force", ws], repo) is not None
        if branch:
            _run(["git", "-C", repo, "branch", "-d", branch], repo)
        return ok

    def current_rev(self, ws: str) -> Rev | None:
        return _run(["git", "-C", ws, "rev-parse", "HEAD"], ws)

    def dirty_files(self, ws: str) -> list[str]:
        out = _run(["git", "-C", ws, "status", "--porcelain"], ws)
        return out.splitlines() if out else []

    def has_changes(self, ws: str, *, since: Rev) -> bool:
        out = _run(["git", "-C", ws, "rev-list", "--count", f"{since}..HEAD"], ws)
        if out is None:
            return True  # unknown — assume work exists (conservative)
        try:
            return int(out or "0") > 0
        except ValueError:
            return True

    def update_to(self, ws: str, base: Rev) -> UpdateResult:
        # Idempotent recovery from a previously interrupted update.
        git_dir = _run(["git", "-C", ws, "rev-parse", "--git-dir"], ws)
        if git_dir is not None:
            gd = git_dir if Path(git_dir).is_absolute() else str(Path(ws) / git_dir)
            if Path(gd, "rebase-merge").exists() or Path(gd, "rebase-apply").exists():
                _run(["git", "-C", ws, "rebase", "--abort"], ws)

        # graphify-out/** routes to merge=ours (.gitattributes) — set the LOCAL
        # driver idempotently so a rebase never conflicts on the large graph.json
        # two branches both regenerate (2026-06-21 concurrent-integrate pileup
        # root cause 2). Config is shared across linked worktrees.
        _run(["git", "-C", ws, "config", "merge.ours.driver", "true"], ws)

        result = subprocess.run(
            ["git", "-C", ws, "rebase", base], capture_output=True, text=True,
        )
        if result.returncode != 0:
            conflicts_out = _run(
                ["git", "-C", ws, "diff", "--name-only", "--diff-filter=U"], ws,
            )
            conflicts = conflicts_out.splitlines() if conflicts_out else []
            _run(["git", "-C", ws, "rebase", "--abort"], ws)
            return UpdateResult(ok=False, conflicts=conflicts, detail=result.stderr.strip())
        return UpdateResult(ok=True, conflicts=[], detail="")

    def describe_changes(self, ws: str, *, since: Rev) -> str:
        out = _run(["git", "-C", ws, "diff", "--stat", f"{since}..HEAD"], ws)
        return (out or "")[:2000]

    # ── publish ──────────────────────────────────────────────────────────────

    def submit(
        self, ws: str, *, base: Rev, mode: SubmitMode, push: bool = True
    ) -> SubmitResult:
        """Get the workspace's tested work onto (or into the queue toward) trunk.

        ``mode="queue"`` pushes the feature branch only (never ff-merges local
        trunk) — the git-pr recipe. ``mode="direct"`` runs git's full
        local-main-sync → ff-merge → (push if ``push``) recipe — ``push=False``
        is the git-mechanics expression of ``push_mode="none"`` (merge locally,
        publish nowhere); a keyword-only growth param (§1.3), non-breaking.
        """
        branch = _run(["git", "-C", ws, "symbolic-ref", "--short", "HEAD"], ws)
        if not branch:
            return SubmitResult(status="failed", detail="cannot resolve workspace branch")

        main_repo = self.primary_root(self.repo_root(ws) or ws)

        # Validate main_repo's checked-out branch matches the expected trunk
        # branch name BEFORE any side effect — for every mode, mirroring the
        # original guard's placement ahead of the push_mode branch (2026-06-14
        # ZA incident: external state left main checked out on a stray branch).
        local_main = _run(["git", "-C", main_repo, "symbolic-ref", "--short", "HEAD"], main_repo) or "main"
        expected_main = base.split("/")[-1]
        if local_main != expected_main:
            return SubmitResult(
                status="failed",
                detail=(
                    f"main_repo_path HEAD is on '{local_main}', expected '{expected_main}'. "
                    f"Check out '{expected_main}' in {main_repo} and re-run integrate."
                ),
            )

        if mode == "queue":
            push_result = subprocess.run(
                ["git", "-C", ws, "push", "origin",
                 f"{branch}:{branch}", "--force-with-lease"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return SubmitResult(status="failed", detail=push_result.stderr.strip())
            return SubmitResult(status="submitted", ticket=branch)

        # Discard local modifications to graphify-out/ before the ff-merge —
        # graphify watch regenerates it on every commit, so a stale local copy
        # blocks a clean ff-only merge (2026-06-11/06-14 incidents).
        if Path(main_repo, "graphify-out").exists():
            _run(["git", "-C", main_repo, "checkout", "--", "graphify-out/"], main_repo)
            _run(["git", "-C", main_repo, "clean", "-fd", "--", "graphify-out/"], main_repo)

        # Sync local main to base BEFORE the ff-merge (2026-06-21 concurrent-
        # integrate pileup root cause 1) — never discard uncommitted tracked work.
        tracked_dirty = _run(
            ["git", "-C", main_repo, "status", "--porcelain", "--untracked-files=no"],
            main_repo,
        )
        if tracked_dirty:
            sync = subprocess.run(
                ["git", "-C", main_repo, "merge", "--ff-only", base],
                capture_output=True, text=True,
            )
            if sync.returncode != 0:
                return SubmitResult(
                    status="failed",
                    detail=(
                        f"local main diverged from {base} with uncommitted tracked "
                        f"changes in {main_repo}; resolve manually then re-run integrate."
                    ),
                )
        else:
            _run(["git", "-C", main_repo, "reset", "--hard", base], main_repo)

        result = subprocess.run(
            ["git", "-C", main_repo, "merge", "--ff-only", branch],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return SubmitResult(
                status="failed",
                detail=f"FF-merge of {branch} failed: {result.stderr.strip()}",
            )

        if push:
            push_result = subprocess.run(
                ["git", "-C", main_repo, "push", "origin", f"{local_main}:{local_main}"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return SubmitResult(status="failed", detail=f"Push failed: {push_result.stderr.strip()}")

        landed_rev = _run(["git", "-C", main_repo, "rev-parse", "HEAD"], main_repo)
        return SubmitResult(status="landed", landed_rev=landed_rev)

    def land_status(self, repo: str, ticket: str) -> LandStatus:
        """git-direct submits always resolve synchronously inside ``submit`` —
        this seam only matters for ``mode="queue"`` (git-pr). Resolving a
        pushed branch to its merged/landed identity (§7.3, DA open question §8.2)
        is Phase-2 scope; fail-closed to unlanded until that lands."""
        return LandStatus(state="unlanded")

    # ── back-compat aliases (kept so the runs-ledger consumers never churn) ──

    def head(self, path: str) -> str | None:
        return self.resolve(path, None)

    def is_dirty(self, path: str) -> bool:
        return bool(self.dirty_files(path))

    def make_safety_branch(self, path: str, sha: str, name: str) -> bool:
        if _run(["git", "branch", name, sha], path) is None:
            return False
        return _run(["git", "switch", name], path) is not None
