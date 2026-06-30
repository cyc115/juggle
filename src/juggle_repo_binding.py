"""Deterministic project→repo binding + integrate source-binding guard.

Single source of truth for resolving which SOURCE repo an agent/thread binds to,
and for refusing to integrate a mis-bound autopilot topic.

Incident (2026-06-16, multi-repo mis-binding cascade): the orchestrator was
launched from the plugin install dir (~/.claude — itself a git repo), so
cwd-derived binding (``git rev-parse --show-toplevel`` on ``os.getcwd()``) tagged
agents/threads with ``/Users/mikechen/.claude`` instead of juggle's real source
at ``~/github/juggle``. integrate then ran against the WRONG repo: the topic
branch was empty there (work dropped) or a stray ff-merge advanced origin/main to
an unrelated branch's HEAD out-of-band. Fix: resolve from ``canonical_repo_path()``
(cwd-INDEPENDENT, anchored on juggle's own ``__file__`` via ``git worktree list``)
and guard integrate against a provably mis-bound repo.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git_toplevel(path: str) -> str:
    """``git -C <path> rev-parse --show-toplevel``; '' if not inside a git repo."""
    if not path:
        return ""
    try:
        return subprocess.check_output(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _is_bad_base(repo: str) -> bool:
    """A base repo we must NEVER bind an agent/worktree to: empty, the ~/.claude
    plugin install dir (or under it), or any toplevel whose basename is '.claude'
    (cheap secondary tripwire for the 2026-06-29 send-task wrong-base incident —
    ``juggle-.claude-*`` worktrees cut from the ~/.claude config repo)."""
    repo = (repo or "").strip()
    if not repo:
        return True
    if is_plugin_install_dir(repo):
        return True
    try:
        return Path(repo).name == ".claude"
    except Exception:
        return False


def _pane_cwd(pane_id: str) -> str:
    """The tmux pane's current working directory — where the dispatched coder
    actually runs. '' when pane_id is falsy or tmux cannot report it. Factored
    as a seam so the pane-cwd anchor is unit-testable without a live tmux."""
    if not pane_id:
        return ""
    try:
        return subprocess.check_output(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_path}"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def pane_repo_path(pane_id: str | None) -> str:
    """Git toplevel of the agent PANE's cwd — the PRIMARY base-repo anchor for a
    dispatched coder (it reflects where the agent will run, NOT where the
    orchestrator's ``__file__`` lives, so a CLI invoked from the installed plugin
    copy never binds to the enclosing ~/.claude repo). Returns '' when the pane
    has no cwd, the cwd is not a git repo, or the result is a bad base."""
    top = _git_toplevel(_pane_cwd(pane_id or ""))
    return "" if _is_bad_base(top) else top


def spawn_repo_path(pane_id: str | None = None) -> str:
    """The canonical SOURCE repo an agent binds to at spawn/dispatch time.

    Resolution order (2026-06-29 fix):
      1. the agent PANE's cwd git toplevel (``pane_repo_path``) when a pane is
         known — the principled anchor for where the coder actually runs;
      2. ``canonical_repo_path()`` (cwd-independent, ``__file__``-anchored) — the
         watchdog path, correct when launched from the dev checkout;
      3. the orchestrator cwd git toplevel.
    Every candidate is rejected if it is a bad base (~/.claude / plugin dir /
    basename '.claude'), so neither the installed-plugin ``__file__`` anchor nor a
    cwd==~/.claude launch can mis-bind the agent (2026-06-16 + 2026-06-29).
    """
    pane = pane_repo_path(pane_id)
    if pane:
        return pane
    try:
        from juggle_watchdog_singleton import canonical_repo_path

        repo = (canonical_repo_path() or "").strip()
        if repo and not _is_bad_base(repo):
            return repo
    except Exception:
        pass
    top = _git_toplevel(os.getcwd())
    return top if top and not _is_bad_base(top) else ""


def canonical_main_ref(repo: str) -> str | None:
    """Return the best canonical main ref for ``repo``.

    Prefers origin/<main> after a targeted fetch so it reflects the pushed
    truth. Falls back to a local main/master if origin is unreachable or absent.
    Returns None if no main ref can be resolved at all.
    """
    # Fetch origin/<main> so the canonical ref reflects the pushed state.
    # Non-fatal: if origin is unreachable we fall through to local refs.
    for branch in ("main", "master"):
        subprocess.run(
            ["git", "-C", repo, "fetch", "origin", branch],
            capture_output=True, text=True,
        )
    for candidate in ("origin/main", "origin/master", "main", "master"):
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", candidate],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return candidate
    return None


def main_worktree_of(repo: str) -> str:
    """Resolve ``repo`` to its primary (main) worktree path, normalized.

    The first entry of ``git worktree list --porcelain`` is always the main
    worktree, so a worktree copy and its source repo collapse to the same value.
    Returns "" if ``repo`` is not a git repo.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("worktree "):
                    return str(Path(line[len("worktree "):].strip()).resolve())
    except Exception:
        pass
    return ""


def is_plugin_install_dir(repo: str) -> bool:
    """True if ``repo`` is the Claude Code plugin install dir (~/.claude) or under
    it — the known-bad binding from the 2026-06-16 incident. Plugin code is
    installed there AND juggle is developed in ~/github/juggle, so a CLAUDE.md /
    commands/*.md present in both made the wrong binding "look" valid.
    """
    try:
        bound = Path(repo).resolve()
        claude_root = (Path.home() / ".claude").resolve()
        return bound == claude_root or claude_root in bound.parents
    except Exception:
        return False


def assert_source_binding(main_repo_path: str, *, is_autopilot: bool) -> str | None:
    """Guard against mis-bound integrate (2026-06-16 multi-repo incident).

    For an AUTOPILOT thread (bound to a graph task), refuse to integrate when
    ``main_repo_path`` is provably mis-bound. Two checks, BOTH targeted to avoid
    false-positives against legitimate external-project autopilots:

      1. ``main_repo_path`` is (under) the Claude plugin install dir ~/.claude —
         the exact bad value from the incident. The topic branch lives there but
         it is NOT the project's source, so a ff-merge/push drops work or
         advances the wrong HEAD. ALWAYS refuse.
      2. When juggle is driving its OWN source (canonical_repo_path() is a juggle
         checkout) the bound repo is also a juggle checkout but a DIFFERENT main
         worktree — a worktree-copy mis-binding. Refuse.

    Returns an error string to refuse, or None if the binding is acceptable.
    Plain (non-project) threads and external-project autopilots are unaffected.
    """
    if not is_autopilot:
        return None
    main_repo_path = (main_repo_path or "").strip()
    if not main_repo_path:
        return None

    # Check 1: the plugin install dir is never a valid integrate target.
    if is_plugin_install_dir(main_repo_path):
        return (
            f"main_repo_path '{main_repo_path}' is the Claude plugin install dir "
            f"(~/.claude), not a project source repo. Refusing to integrate a "
            f"mis-bound autopilot topic (2026-06-16 multi-repo incident): a "
            f"ff-merge/push here drops the work or advances the wrong HEAD. "
            f"Re-dispatch the topic so it binds to the canonical source."
        )

    # Check 2: juggle driving its own source must bind to the canonical worktree.
    try:
        from juggle_watchdog_singleton import canonical_repo_path
        canonical = (canonical_repo_path() or "").strip()
    except Exception:
        return None
    if not canonical:
        return None
    canonical_main = main_worktree_of(canonical)
    if not canonical_main or not Path(canonical_main, "src", "juggle_cli.py").exists():
        return None  # canonical isn't a juggle source checkout — nothing to compare

    bound_main = main_worktree_of(main_repo_path)
    bound_is_juggle = bool(bound_main) and Path(bound_main, "src", "juggle_cli.py").exists()
    if bound_is_juggle and bound_main != canonical_main:
        return (
            f"main_repo_path '{main_repo_path}' (source={bound_main}) is a juggle "
            f"checkout but NOT the canonical source '{canonical_main}'. Refusing to "
            f"integrate a mis-bound autopilot topic (2026-06-16 multi-repo incident)."
        )
    return None
