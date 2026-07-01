"""Regression pins for the orchestration-metrics layer (2026-06-30)."""
from datetime import datetime, timezone

import juggle_run_tokens as rt
import juggle_metrics_errors as me


def test_window_excludes_record_one_second_outside(tmp_path):
    """2026-06-30 orchestration-metrics: a record 1s outside the window is NOT summed."""
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"assistant","timestamp":"2026-06-30T12:05:00.000Z",'
        '"message":{"usage":{"input_tokens":500,"output_tokens":50,'
        '"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n'
    )
    # window STARTS 1s after the record → the record is excluded → all zeros.
    start = datetime(2026, 6, 30, 12, 5, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc)
    assert rt.sum_usage_in_window(p, start, end) == {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def test_malformed_jsonl_line_never_raises(tmp_path):
    """2026-06-30 orchestration-metrics: a malformed JSONL line is skipped, not fatal."""
    p = tmp_path / "t.jsonl"
    p.write_text(
        "this is not json\n"
        '{"type":"assistant","timestamp":"2026-06-30T12:05:00.000Z",'
        '"message":{"usage":{"input_tokens":7,"output_tokens":1,'
        '"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}}\n'
        "{bad json again\n"
    )
    start = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc)
    assert rt.sum_usage_in_window(p, start, end) == {
        "input": 7, "output": 1, "cache_read": 0, "cache_write": 0}


def test_rework_keys_on_redispatch_only(juggle_db):
    """2026-06-30 orchestration-metrics: a planned 2-node graph (two DISTINCT
    task_ids, each dispatched once) yields rework==0 — the false-positive guard."""
    runs = [
        {"task_id": "nodeA", "status": "completed"},
        {"task_id": "nodeB", "status": "completed"},
    ]
    assert me.error_breakdown(juggle_db, runs)["rework"] == 0


def test_close_run_backfill_idempotent_safe(juggle_db, monkeypatch):
    """2026-06-30 orchestration-metrics: a second close_run on an already-closed
    thread finds no open run and does not corrupt the token columns."""
    monkeypatch.setattr(rt, "read_run_tokens",
                        lambda run, **k: {"input": 9, "output": 8, "cache_read": 7, "cache_write": 6})
    tid = juggle_db.create_thread(topic="t", session_id="s")
    rid = juggle_db.insert_agent_run(thread_id=tid, input_prompt="p", agent_id=None,
                                     role="coder", model="m", harness="claude",
                                     project_id="INBOX", topic_id=None, task_id="t1")
    juggle_db.close_run(tid, output="done", diffstat=None, status="completed")
    juggle_db.close_run(tid, output="again", diffstat=None, status="completed")  # no open run
    run = juggle_db.get_run(rid)
    assert (run["input_tokens"], run["output_tokens"], run["cache_read_tokens"],
            run["cache_write_tokens"]) == (9, 8, 7, 6)
    assert run["output"] == "done"  # untouched by the second no-op close
