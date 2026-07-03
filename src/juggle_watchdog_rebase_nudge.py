"""juggle_watchdog_rebase_nudge — early-rebase advisory nudge (P3, 2026-07-03).

A running topic whose trunk has advanced far since its worktree forked is
heading for an expensive late rebase. This sweep (pattern-matched on
``sweep_stale_claims``) nudges the topic's agent ONCE per running episode
when trunk has moved >= REBASE_NUDGE_THRESHOLD commits ahead of the
worktree's actual fork point (``git merge-base``) — advisory only, reuses the
existing steering-message path (``mgr.send_message``, same primitive as
``juggle_watchdog_stall``).

Single-nudge-only: ``nodes.rebase_nudge_sent`` gates re-sending for the same
running episode; ``clear_rebase_nudge`` resets it at (re-)dispatch.
"""
from __future__ import annotations

import logging
import subprocess
from typing import Any

_log = logging.getLogger("juggle-watchdog")

REBASE_NUDGE_THRESHOLD = 5

REBASE_NUDGE_TEXT = (
    "Advisory: trunk has moved {n} commits ahead of your branch's fork point. "
    "Consider rebasing early to avoid a large late conflict — not required, "
    "integrate will rebase for you at land time."
)


def commits_behind_trunk(repo: str, branch: str, trunk: str) -> int | None:
    """How many commits ``trunk`` has advanced since ``branch`` forked from it.

    Uses the branch's actual merge-base with trunk (not a stored snapshot) —
    self-contained, no extra DB state needed for the count itself. None on
    any git error (missing branch/repo) — fail-soft, never blocks the tick.
    """
    try:
        merge_base = subprocess.run(
            ["git", "-C", repo, "merge-base", trunk, branch],
            capture_output=True, text=True,
        )
        if merge_base.returncode != 0:
            return None
        base = merge_base.stdout.strip()
        count = subprocess.run(
            ["git", "-C", repo, "rev-list", "--count", f"{base}..{trunk}"],
            capture_output=True, text=True,
        )
        if count.returncode != 0:
            return None
        return int(count.stdout.strip())
    except Exception:
        return None


def _running_topics_with_worktree(db) -> list[dict]:
    """Running topics bound to a thread carrying live worktree fields."""
    out = []
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT n.id AS topic_id, n.rebase_nudge_sent AS rebase_nudge_sent, "
            "  (SELECT depends_on_id FROM node_edges "
            "     WHERE node_id = n.id AND kind = 'dispatch' LIMIT 1) AS thread_id "
            "FROM nodes n WHERE n.kind = 'topic' AND n.state = 'running'"
        ).fetchall()
        for row in rows:
            d = dict(row)
            if not d.get("thread_id"):
                continue
            thread = db.get_thread(d["thread_id"])
            if not thread:
                continue
            if not (thread.get("worktree_path") and thread.get("worktree_branch")
                    and thread.get("main_repo_path")):
                continue
            d["thread"] = thread
            out.append(d)
    return out


def clear_rebase_nudge(db, topic_id: str) -> None:
    """Reset the single-nudge gate — call at (re-)dispatch of a topic.

    Fail-soft: never blocks a dispatch (callers need no try/except)."""
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE nodes SET rebase_nudge_sent = 0 WHERE id = ?", (topic_id,)
            )
            conn.commit()
    except Exception:
        pass


def _mark_nudge_sent(db, topic_id: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET rebase_nudge_sent = 1 WHERE id = ?", (topic_id,)
        )
        conn.commit()


def sweep_rebase_nudges(db, mgr: Any) -> list[str]:
    """One tick's worth of early-rebase nudges. Returns nudged topic ids.

    Fail-soft at the top level too (caller — the watchdog tick — must never
    crash because of this sweep)."""
    from vcs import backend_for

    nudged: list[str] = []
    try:
        rows = _running_topics_with_worktree(db)
    except Exception:
        _log.exception("rebase nudge: topic scan failed")
        return nudged
    for row in rows:
        if row.get("rebase_nudge_sent"):
            continue
        thread = row["thread"]
        repo = thread["main_repo_path"]
        branch = thread["worktree_branch"]
        try:
            backend = backend_for(repo)
            trunk = backend.trunk(repo)
        except Exception:
            continue
        if not trunk:
            continue
        n = commits_behind_trunk(repo, branch, trunk)
        if n is None or n < REBASE_NUDGE_THRESHOLD:
            continue

        agent = next(
            (a for a in db.get_all_agents()
             if a.get("assigned_thread") == thread["id"] and a.get("status") == "busy"),
            None,
        )
        if not agent:
            continue
        pane_id = agent.get("pane_id") or ""
        try:
            mgr.send_message(pane_id, REBASE_NUDGE_TEXT.format(n=n))
        except Exception as exc:
            _log.warning("rebase nudge: send failed for topic %s: %s", row["topic_id"], exc)
            continue
        _mark_nudge_sent(db, row["topic_id"])
        nudged.append(row["topic_id"])
    return nudged
