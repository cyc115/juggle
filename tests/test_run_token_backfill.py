"""close_run token backfill (2026-06-30 orchestration-metrics Task 4)."""


def test_close_run_backfills_tokens(juggle_db, monkeypatch):
    """2026-06-30 orchestration-metrics: close_run backfills window-summed tokens."""
    import juggle_run_tokens as rt
    monkeypatch.setattr(rt, "read_run_tokens",
                        lambda run, **k: {"input": 111, "output": 22, "cache_read": 3, "cache_write": 4})
    tid = juggle_db.create_thread(topic="t", session_id="s")
    rid = juggle_db.insert_agent_run(thread_id=tid, input_prompt="p", agent_id=None,
                                     role="coder", model="m", harness="claude",
                                     project_id="INBOX", topic_id=None, task_id="t1")
    juggle_db.close_run(tid, output="done", diffstat=None, status="completed")
    run = juggle_db.get_run(rid)
    assert (run["input_tokens"], run["output_tokens"], run["cache_read_tokens"],
            run["cache_write_tokens"]) == (111, 22, 3, 4)
