"""Fix 2 — reconcile must never erase a recorded FAILURE verdict.

Incident (2026-07-03 integrate-wedge RCA, secondary aggravator §Q2.3):
``reconcile_topic_state`` guarded only ``verified`` from re-derivation. A topic
whose tasks are all terminal-``verified`` but whose integrate FAILED
(``failed-integration``) would be re-derived back to ``integrating`` on the next
reconcile — erasing the failure signal AND the repair-sweep trigger
(``run_repair_sweeps`` only picks up ``failed-integration`` + ``fail_envelope``).

Also pins the amended ``_heal_merged_sha`` (2026-07-03 watchdog-reconciliation
amendment): it heals a REBASED landing (branch not an ancestor of main, work on
main under a different sha) via the two-tier landed oracle — ancestry-only could
not, so a rebased-landed topic wedged at 'integrating' forever.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_db(tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _seed_topic(db, topic_id, task_states, *, state, merged_sha=None,
                thread_id=None):
    from dbops import db_topics, db_graph
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db_topics.create_topic(db, topic_id=topic_id, project_id="INBOX",
                           title=f"Topic {topic_id}")
    with db._connect() as c:
        c.execute(
            "UPDATE nodes SET state=?, dispatch_thread_id=?, merged_sha=? "
            "WHERE id=? AND kind='topic'",
            (state, thread_id, merged_sha, topic_id),
        )
        c.commit()
    if thread_id:
        db_topics.set_topic_thread(db, topic_id, thread_id)
    for i, st in enumerate(task_states):
        tid = f"{topic_id}-t{i}"
        db_graph.create_task(db, task_id=tid, project_id="INBOX", title=tid, prompt="x")
        db_graph.set_task_topic(db, tid, topic_id)
        with db._connect() as c:
            c.execute("UPDATE nodes SET state=? WHERE id=?", (st, tid))
            c.commit()


def test_failed_integration_not_rederived_to_integrating(tmp_path):
    """All tasks verified but topic is failed-integration → reconcile keeps
    failed-integration (does NOT re-derive 'integrating' and erase the verdict)."""
    from dbops.db_topics import reconcile_topic_state, get_topic

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified", "verified"], state="failed-integration",
                merged_sha=None)

    state = reconcile_topic_state(db, "T1")

    assert state == "failed-integration"
    assert get_topic(db, "T1")["state"] == "failed-integration"


def test_failed_verify_not_rederived(tmp_path):
    """A failed-verify verdict is likewise preserved against re-derivation."""
    from dbops.db_topics import reconcile_topic_state, get_topic

    db = _make_db(tmp_path)
    _seed_topic(db, "T1", ["verified"], state="failed-verify", merged_sha=None)

    assert reconcile_topic_state(db, "T1") == "failed-verify"
    assert get_topic(db, "T1")["state"] == "failed-verify"


# ── amended _heal_merged_sha: recognise a REBASED landing ────────────────────


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def test_heal_merged_sha_recognises_rebased_landing(tmp_path):
    """A topic whose branch landed via rebase (equivalent commit on main under a
    different sha, branch tip NOT an ancestor) must heal to 'verified' — the
    ancestry-only heal left it wedged at 'integrating' forever."""
    from dbops.db_topics import reconcile_topic_state, get_topic

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base").write_text("base")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "cyc_X")
    (repo / "feat").write_text("work")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat")
    _git(repo, "checkout", "main")
    (repo / "mainside").write_text("mainside")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "mainside")
    _git(repo, "cherry-pick", "cyc_X")  # rebased landing under a new sha

    db = _make_db(tmp_path)
    tid = db.create_thread(topic="heal-test", session_id="s")
    db.update_thread(tid, main_repo_path=str(repo), worktree_branch="cyc_X")
    _seed_topic(db, "T1", ["verified"], state="integrating", thread_id=tid,
                merged_sha=None)
    with db._connect() as c:
        c.execute("UPDATE nodes SET worktree_branch=?, main_repo_path=? "
                  "WHERE id=?", ("cyc_X", str(repo), "T1"))
        c.commit()

    state = reconcile_topic_state(db, "T1")

    assert state == "verified", "rebased-landed work must heal to verified"
    healed = (get_topic(db, "T1")["merged_sha"] or "").strip()
    assert healed, "a real merged_sha must be stamped"
