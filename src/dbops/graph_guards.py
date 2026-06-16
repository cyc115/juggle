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
    """True iff ``branch`` is an ancestor of ``main`` in ``repo``.

    A missing branch (already merged + deleted by integrate, which clears
    worktree_branch) is treated as merged: the branch ref is gone precisely
    because integrate landed it. A repo we cannot inspect is NOT merged
    (fail-closed — better to keep a topic pre-verified than mark it done).
    """
    if not repo or not Path(repo).exists():
        return False
    if not branch:
        # Fail-closed: no recorded branch means nothing proven merged to main.
        # A topic may only verify when its branch is a proven ancestor of main.
        return False
    # Branch ref gone → integrate deleted it after a successful merge.
    if not _git_ok(["rev-parse", "--verify", branch], repo):
        return _git_ok(["rev-parse", "--verify", main], repo)
    return _git_ok(["merge-base", "--is-ancestor", branch, main], repo)


def topic_is_merged(db, topic_id: str, *, main: str = "main") -> bool:
    """G1: is the topic's work merged into ``main``?

    Resolves the topic's bound thread → (worktree_branch, main_repo_path) and
    asks git. Topics with no bound thread/repo cannot be proven merged and are
    NOT considered merged (fail-closed).
    """
    from dbops import db_topics

    topic = db_topics.get_topic(db, topic_id)
    if topic is None:
        return False
    thread_id = topic.get("thread_id")
    if not thread_id:
        return False
    try:
        thread = db.get_thread(thread_id) or {}
    except Exception:
        thread = {}
    repo = (thread.get("main_repo_path") or "").strip()
    branch = (thread.get("worktree_branch") or "").strip()
    if not repo:
        return False
    return branch_merged_to_main(repo, branch, main=main)


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
