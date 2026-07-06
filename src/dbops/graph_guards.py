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
import re
from pathlib import Path

from vcs import backend_for

# The single shared operational DB. Agents must never migrate it.
SHARED_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


# ---------------------------------------------------------------------------
# G1 — verified ⟺ merged to main
# ---------------------------------------------------------------------------

def _backend_for_fail_closed(repo: str):
    """``backend_for`` is fail-loud (raises on an undetectable repo); every
    caller here is fail-CLOSED (returns False/'' on any error), so swallow it."""
    try:
        return backend_for(repo)
    except Exception:
        return None


def _repo_trunk(repo: str) -> str:
    """Configured trunk/branch name the ancestor gate checks against (SPEC
    2026-07-05 git-ism sweep): juggle_repo_vcs.repo_trunk, lazily imported +
    fail-soft to git's "main" so this deep-dbops guard module stays importable
    without a config import cycle."""
    try:
        from juggle_repo_vcs import repo_trunk
        return repo_trunk(repo)
    except Exception:
        return "main"


def branch_merged_to_main(repo: str, branch: str, *, main: str | None = None) -> bool:
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
    backend = _backend_for_fail_closed(repo)
    if backend is None:
        return False
    if backend.resolve(repo, branch) is None:
        return False  # branch ref gone — NOT proof of merge (fail-closed)
    if main is None:
        main = _repo_trunk(repo)
    return backend.is_ancestor(repo, branch, main)


def resolve_branch_sha(repo: str, branch: str) -> str:
    """Return the commit sha ``branch`` points to in ``repo``, or '' if it
    can't be resolved (no repo, no branch, deleted ref, git error)."""
    if not repo or not branch or not Path(repo).exists():
        return ""
    backend = _backend_for_fail_closed(repo)
    if backend is None:
        return ""
    return backend.resolve(repo, branch) or ""


def sha_is_ancestor(repo: str, sha: str, *, main: str | None = None) -> bool:
    """True iff commit ``sha`` exists in ``repo`` and is an ancestor of ``main``.

    The single source of truth for 'verified ⟺ merged'. Fail-closed on a
    missing repo / empty sha / git error.
    """
    if not repo or not sha or not Path(repo).exists():
        return False
    backend = _backend_for_fail_closed(repo)
    if backend is None:
        return False
    if main is None:
        main = _repo_trunk(repo)
    return backend.is_ancestor(repo, sha, main)


def thread_solely_bound_to(db, thread_id: str, topic_id: str) -> bool:
    """True iff ``thread_id`` is dispatch-bound to ``topic_id`` and to no
    OTHER node (T-fix-backfill-sha-misattribution). dispatch_edge only
    enforces "a node binds exactly one thread", never the reverse — a
    conversation thread reused as the dispatch home for a second, unrelated
    topic leaves BOTH topics' edges live, so its ``worktree_branch`` reflects
    whichever last dispatched through it. Only when exactly one topic (this
    one) is bound is the thread's live branch field still trustworthy as a
    fallback for a topic with no durably-recorded ``worktree_branch`` of its
    own. Fail-closed on any error."""
    if not thread_id or not topic_id:
        return False
    try:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT node_id FROM node_edges "
                "WHERE kind='dispatch' AND depends_on_id=?",
                (thread_id,),
            ).fetchall()
        node_ids = {r[0] for r in rows}
        return node_ids == {topic_id}
    except Exception:
        return False


def _resolve_topic_repo(db, topic: dict) -> str:
    """Resolve the repo that holds ``main`` for this topic's merged_sha check.

    Primary: the bound thread's main_repo_path. Next: the topic NODE's own
    ``main_repo_path`` (stamped at dispatch) — it SURVIVES ``_run_integrate``
    clearing the thread's fields on a successful land, so a topic verified /
    re-integrated after its thread is torn down is still repo-resolvable (the
    2026-07-03 integrate-wedge re-integrate driver needs this to complete an
    external-repo topic to 'verified'; mirrors orphan_guard._node_repo, which
    already reads the node's main_repo_path). Next: the topic's own
    ``pending_merged_repo`` breadcrumb (2026-07-02 async-land incident) — the
    repo a resolved-but-unproven SHA was recorded in. Last resort: juggle's own
    repo — a recorded merged_sha for a self-repo topic must still be checkable.
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
    node_repo = (topic.get("main_repo_path") or "").strip()
    if node_repo:
        return node_repo
    pending_repo = (topic.get("pending_merged_repo") or "").strip()
    if pending_repo:
        return pending_repo
    try:
        from juggle_cli_common import SRC_DIR
        return str(Path(SRC_DIR).parent.resolve())
    except Exception:
        return ""


def topic_is_merged(db, topic_id: str, *, main: str | None = None) -> bool:
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


_MISSING_SCHEMA_RE = re.compile(r"no such (table|column)", re.IGNORECASE)


def diagnose_agent_db_error(exc: BaseException) -> str:
    """Rewrite a misleading 'DB needs migration' diagnosis for agent contexts.

    G2 (above) means an agent NEVER migrates the shared DB, so a
    ``sqlite3.OperationalError`` about a missing table/column hit from an
    agent context cannot mean "the DB is behind" — assert_migration_allowed
    already forbids that agent from being the one to fix it. It can only mean
    the agent is running a juggle_cli.py OLDER than the schema the shared DB
    was migrated to. 2026-07-03 incidents CR + CO: agents on the stale
    installed-plugin-cache CLI hit exactly this and self-diagnosed "DB is
    pre-Migration-39" — backwards, and it burned two topics. Returns the
    corrected message string (never raises); non-agent contexts and
    non-schema errors pass through unchanged.
    """
    import sqlite3

    text = str(exc)
    if not isinstance(exc, sqlite3.OperationalError):
        return text
    if not is_agent_context():
        return text
    if not _MISSING_SCHEMA_RE.search(text):
        return text

    repo_root = os.environ.get("JUGGLE_REPO_ROOT") or str(
        Path(__file__).resolve().parent.parent.parent
    )
    return (
        f"{text} — your CLI is stale (installed-plugin-cache path is behind "
        f"the shared DB's schema); re-run via {repo_root}/src/juggle_cli.py "
        f"instead. NOT a sign the schema is missing anything — agents never "
        f"run schema changes against the shared DB (G2)."
    )


# ── two-tier LANDED oracle (extracted to dbops.landed, LOC gate) ─────────────
# Re-exported so the existing graph_guards.resolve_landed_sha call/patch surface
# is unchanged. The import sits at the bottom, after the ancestry primitives it
# depends on are defined, so the dbops.landed → graph_guards back-import resolves.
from dbops.landed import (  # noqa: E402,F401
    branch_content_landed,
    resolve_landed_sha,
)
