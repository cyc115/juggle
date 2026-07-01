"""insert_agent_run stamps prompt identity + agent_cwd (2026-06-30 orchestration-metrics Task 3)."""
import juggle_prompt_metrics as pm


def test_insert_stamps_prompt_identity(juggle_db):
    """2026-06-30 orchestration-metrics: insert_agent_run persists fingerprint/version/bytes."""
    tid = juggle_db.create_thread(topic="t", session_id="s")
    fp = pm.prompt_fingerprint("FULL PROMPT")
    rid = juggle_db.insert_agent_run(
        thread_id=tid, input_prompt="FULL PROMPT", agent_id=None, role="coder",
        model="m", harness="claude", project_id="INBOX", topic_id=None, task_id="t1",
        prompt_fingerprint=fp, prompt_version="v1", prompt_bytes=pm.prompt_bytes_of("FULL PROMPT"))
    run = juggle_db.get_run(rid)
    assert run["prompt_fingerprint"] == fp and run["prompt_version"] == "v1"
    assert run["prompt_bytes"] == 11


def test_insert_stamps_agent_cwd(juggle_db):
    """2026-06-30 orchestration-metrics: insert_agent_run persists agent_cwd (worktree)."""
    tid = juggle_db.create_thread(topic="t", session_id="s")
    rid = juggle_db.insert_agent_run(
        thread_id=tid, input_prompt="p", agent_id=None, role="coder",
        model="m", harness="claude", project_id="INBOX", topic_id=None, task_id="t1",
        agent_cwd="/private/tmp/juggle-juggle-Q")
    assert juggle_db.get_run(rid)["agent_cwd"] == "/private/tmp/juggle-juggle-Q"
