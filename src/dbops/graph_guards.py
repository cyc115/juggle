"""dbops.graph_guards — durable invariants for the autopilot graph machinery.

Born from the 2026-06-13 incident (docs/incidents/2026-06-13-autopilot-shared-db-
corruption.md): unmerged WIP migrations mutated the shared prod DB and a feature
was marked ``verified`` while sitting unmerged on a ``cyc_*`` branch.

Owns the two cross-cutting code guards that the rest of the graph store calls
into:

* ``topic_is_merged`` (G1) — a topic may only become ``verified`` when its bound
  branch is an ancestor of ``main`` (``git merge-base --is-ancestor``).
* ``shared_prod_db`` / ``agent_context`` / ``assert_migration_allowed`` (G2) —
  an AGENT/worktree process must never migrate the shared production DB.

Must not own: any state transition (that stays in db_graph/db_topics), any CLI
parsing. Pure predicates + one raising assertion.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The single shared operational DB. Agents must never migrate it.
SHARED_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


# ---------------------------------------------------------------------------
# G1 — verified ⟺ merged to main
# ---------------------------------------------------------------------------

def _git_ok(args: list[str], cwd: str) -> bool:
    try:
        return (
            subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True, text=True, timeout=10,
            ).returncode
            == 0
        )
    except Exception:
        return False


def branch_merged_to_main(repo: str, branch: str, *, main: str = "main") -> bool:
    """True iff ``branch`` is a LIVE ref that is an ancestor of ``main``.

    Strictly fail-closed (T-verified-merged-sha, 2026-06-16): the old
    "no branch → merged" and "branch ref gone → merged" fail-open paths caused
    false-verified 3× and are REMOVED. A missing/empty branch, a deleted branch
    ref, or an uninspectable repo all return False. The authoritative
    verified-gate is now ``sha_is_ancestor`` over a recorded ``merged_sha``;
    this helper is retained only as a pure git-ancestry predicate.
    """
    if not repo or not Path(repo).exists():
        return False
    if not branch:
        return False
    if not _git_ok(["rev-parse", "--verify", branch], repo):
        return False  # branch ref gone — NOT proof of merge (fail-closed)
    return _git_ok(["merge-base", "--is-ancestor", branch, main], repo)


def resolve_branch_sha(repo: str, branch: str) -> str:
    """Return the commit sha ``branch`` points to in ``repo``, or '' if it
    can't be resolved (no repo, no branch, deleted ref, git error)."""
    if not repo or not branch or not Path(repo).exists():
        return ""
    try:
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", branch],
            capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def sha_is_ancestor(repo: str, sha: str, *, main: str = "main") -> bool:
    """True iff commit ``sha`` exists in ``repo`` and is an ancestor of ``main``.

    The single source of truth for 'verified ⟺ merged'. Fail-closed on a
    missing repo / empty sha / git error.
    """
    if not repo or not sha or not Path(repo).exists():
        return False
    return _git_ok(["merge-base", "--is-ancestor", sha, main], repo)


def _resolve_topic_repo(db, topic: dict) -> str:
    """Resolve the repo that holds ``main`` for this topic's merged_sha check.

    Primary: the bound thread's main_repo_path. Fallback: juggle's own repo —
    integrate clears main_repo_path on success, but a recorded merged_sha for a
    self-repo topic must still be checkable afterward (and on orphan recovery).
    """
    thread_id = topic.get("thread_id")
    if thread_id:
        try:
            thread = db.get_thread(thread_id) or {}
        except Exception:
            thread = {}
        repo = (thread.get("main_repo_path") or "").strip()
        if repo:
            return repo
    try:
        from juggle_cli_common import SRC_DIR
        return str(Path(SRC_DIR).parent.resolve())
    except Exception:
        return ""


def topic_is_merged(db, topic_id: str, *, main: str = "main") -> bool:
    """G1 single gate: a topic is merged IFF it has a recorded ``merged_sha``
    that is an ancestor of ``main``. Nothing else.

    No branch-ref heuristics, no fail-open: a NULL merged_sha is never merged,
    closing the empty-branch / branch-gone / orphan-bypass holes at the source.
    """
    from dbops import db_topics

    topic = db_topics.get_topic(db, topic_id)
    if topic is None:
        return False
    sha = (topic.get("merged_sha") or "").strip()
    if not sha:
        return False
    repo = _resolve_topic_repo(db, topic)
    if not repo:
        return False
    return sha_is_ancestor(repo, sha, main=main)


# ---------------------------------------------------------------------------
# G2 — agents must not migrate the shared prod DB
# ---------------------------------------------------------------------------

def is_agent_context() -> bool:
    """True when running inside a dispatched AGENT / worktree process.

    JUGGLE_ORCHESTRATOR=1 is the authoritative orchestrator/watchdog identity
    flag. It wins over ALL other signals (cwd heuristic, JUGGLE_IS_AGENT) so the
    watchdog daemon — which may be spawned while cwd is inside a juggle worktree
    — is never mistaken for an agent. Only orchestrator/daemon code sets this.

    Two agent signals, either sufficient (when orchestrator marker absent):
      * ``JUGGLE_IS_AGENT=1`` — exported by juggle_harness into every dispatched
        agent's env (the authoritative, harness-independent identity flag).
      * cwd under a ``juggle-juggle-*`` worktree tmp dir — defence in depth for
        any agent spawned outside the harness env prefix.
    """
    # Orchestrator marker wins unconditionally — watchdog/daemon sets this.
    if os.environ.get("JUGGLE_ORCHESTRATOR") == "1":
        return False
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        return True
    if os.environ.get("JUGGLE_AGENT_WORKTREE"):
        return True
    try:
        cwd = str(Path.cwd())
    except Exception:
        cwd = ""
    return "juggle-juggle-" in cwd


def is_shared_prod_db(db_path) -> bool:
    """True iff ``db_path`` resolves to the shared operational DB."""
    if not db_path:
        return False
    try:
        return Path(db_path).resolve() == SHARED_PROD_DB
    except Exception:
        return False


class SharedDBMigrationRefused(RuntimeError):
    """An agent/worktree process tried to migrate the shared prod DB."""


def assert_migration_allowed(db_path) -> None:
    """G2 gate: refuse migrating the shared prod DB from an agent context.

    Only the orchestrator (non-agent) migrates the shared DB. Agents run against
    an isolated DB or skip migration entirely. Raises SharedDBMigrationRefused.
    """
    if is_shared_prod_db(db_path) and is_agent_context():
        raise SharedDBMigrationRefused(
            f"refusing to migrate the shared production DB {db_path} from an "
            f"agent/worktree context (JUGGLE_IS_AGENT/worktree cwd). Only the "
            f"orchestrator migrates the shared DB; agents use an isolated DB."
        )
