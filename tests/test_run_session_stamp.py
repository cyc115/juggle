"""Stop-hook session_id stamp, Tier-1b (2026-06-30 orchestration-metrics Task 5)."""


def test_set_run_session_id_stamps_open_run(juggle_db):
    """2026-06-30 orchestration-metrics: Stop-hook stamps session_id (Tier-1b)."""
    tid = juggle_db.create_thread(topic="t", session_id="s")
    rid = juggle_db.insert_agent_run(thread_id=tid, input_prompt="p", agent_id="ag-1",
                                     role="coder", model="m", harness="claude",
                                     project_id="INBOX", topic_id=None, task_id="t1")
    juggle_db.set_run_session_id("ag-1", "sess-xyz")
    assert juggle_db.get_run(rid)["session_id"] == "sess-xyz"
    juggle_db.set_run_session_id("ag-1", "other")  # already set → no-op
    assert juggle_db.get_run(rid)["session_id"] == "sess-xyz"


def test_stamp_agent_session_matches_cwd_to_worktree(juggle_db):
    """2026-06-30 orchestration-metrics: the Stop-hook resolver stamps the open run
    of the busy agent whose worktree == the hook's cwd (orchestrator cwd matches none)."""
    from juggle_hooks_prompt import _stamp_agent_session

    thread = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.update_thread(thread, worktree_path="/private/tmp/juggle-juggle-Q")
    aid = juggle_db.create_agent(role="coder", pane_id="%1")
    juggle_db.update_agent(aid, assigned_thread=thread, status="busy")
    rid = juggle_db.insert_agent_run(thread_id=thread, input_prompt="p", agent_id=aid,
                                     role="coder", model="m", harness="claude",
                                     project_id="INBOX", topic_id=None, task_id="t1")
    _stamp_agent_session(juggle_db, {"session_id": "child-sess",
                                     "cwd": "/private/tmp/juggle-juggle-Q"})
    assert juggle_db.get_run(rid)["session_id"] == "child-sess"


def test_stamp_agent_session_noop_on_non_matching_cwd(juggle_db):
    """2026-06-30 orchestration-metrics: a cwd matching no busy agent's worktree
    (e.g. the orchestrator's main repo) stamps nothing."""
    from juggle_hooks_prompt import _stamp_agent_session

    thread = juggle_db.create_thread(topic="feature", session_id="s")
    juggle_db.update_thread(thread, worktree_path="/private/tmp/juggle-juggle-Q")
    aid = juggle_db.create_agent(role="coder", pane_id="%1")
    juggle_db.update_agent(aid, assigned_thread=thread, status="busy")
    rid = juggle_db.insert_agent_run(thread_id=thread, input_prompt="p", agent_id=aid,
                                     role="coder", model="m", harness="claude",
                                     project_id="INBOX", topic_id=None, task_id="t1")
    _stamp_agent_session(juggle_db, {"session_id": "orch", "cwd": "/Users/m/github/juggle"})
    assert juggle_db.get_run(rid)["session_id"] is None
