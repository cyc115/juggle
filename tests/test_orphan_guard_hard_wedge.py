"""Fix 5 — the G5 orphan guard must escalate a HARD-wedged 'integrating' topic
even when its dispatch agent still shows 'busy'.

Incident (2026-07-03 integrate-wedge RCA §Q3/fix 5): three topics sat in
state='integrating' for 1–1.5 h while their (stuck) coder agents still showed
'busy'. The G5 guard's finalize-race protection (skip a topic whose owning agent
is still busy, 2026-07-02) then suppressed detection FOREVER — zero action items
for a 1.5 h wedge. The busy-agent skip is right for the first few minutes (an
agent mid-finalize) but a topic 'integrating' well past a hard-wedge window with
a still-'busy' agent is a STUCK agent, not a finalizing one, and must surface a
HIGH blocker. The finalize-race guard (recent verified + busy agent) must NOT
regress.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _repo_with_unmerged_branch(repo, branch="cyc_X"):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", branch)
    (repo / "f.txt").write_text("work")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "work")
    _git(repo, "checkout", "main")
    return branch


def _bind_node_branch(db, node_id, *, repo, branch):
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET worktree_branch=?, main_repo_path=? WHERE id=?",
            (branch, str(repo), node_id),
        )
        c.commit()


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state="integrating",
                thread_id=None, merged_sha=None, updated_at=None):
    from dbops import db_topics, db_graph

    now = updated_at or datetime.now(timezone.utc).isoformat()
    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET state=?, dispatch_thread_id=?, merged_sha=?, "
            "updated_at=? WHERE id=? AND kind='topic'",
            (state, thread_id, merged_sha, now, topic_id),
        )
        c.commit()
    if thread_id:
        db_topics.set_topic_thread(db, topic_id, thread_id)
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, topic_id)
        with db._connect() as c:
            c.execute("UPDATE nodes SET state=?, verified_at=? WHERE id=?",
                      (st, now, tid))
            c.commit()
    # Stamp the topic's updated_at LAST — set_topic_thread / set_task_topic above
    # refresh it, and _hard_wedged reads it as the time it entered 'integrating'.
    with db._connect() as c:
        c.execute("UPDATE nodes SET updated_at=? WHERE id=? AND kind='topic'",
                  (now, topic_id))
        c.commit()


def _make_agent_busy(db, thread_id):
    agent_id = db.create_agent(role="coder", pane_id="pane-1")
    assert db.cas_assign_agent(agent_id, thread_id)
    return agent_id


def test_hard_wedged_integrating_topic_flagged_despite_busy_agent(tmp_path):
    """1.5 h wedge with a still-'busy' agent → HIGH action item (the incident)."""
    from dbops import orphan_guard

    repo = tmp_path / "repo"
    branch = _repo_with_unmerged_branch(repo)

    db = _make_db(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    _seed_topic(db, "T1", ["verified"], state="integrating", thread_id="thr-1",
                merged_sha=None, updated_at=stale)
    _bind_node_branch(db, "T1", repo=repo, branch=branch)
    _make_agent_busy(db, "thr-1")   # agent stuck 'busy' (the suppression trigger)

    orphans = orphan_guard.find_unmerged_completed_topics(db)
    assert [t["id"] for t in orphans] == ["T1"]

    flagged = orphan_guard.flag_unmerged_completed_topics(db)
    assert flagged == ["T1"]
    items = db.get_open_action_items()
    assert any(i["priority"] == "high" and "T1" in i["message"] for i in items)


def test_recent_integrating_with_busy_agent_still_suppressed(tmp_path):
    """Finalize-race guard preserved: a topic that JUST reached integrating with
    a busy agent (agent mid-finalize) must NOT be flagged yet."""
    from dbops import orphan_guard

    db = _make_db(tmp_path)
    fresh = datetime.now(timezone.utc).isoformat()
    _seed_topic(db, "T1", ["verified"], state="integrating", thread_id="thr-1",
                merged_sha=None, updated_at=fresh)
    _make_agent_busy(db, "thr-1")

    assert orphan_guard.find_unmerged_completed_topics(db) == []
    assert orphan_guard.flag_unmerged_completed_topics(db) == []
    assert db.get_open_action_items() == []
