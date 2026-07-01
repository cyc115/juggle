"""read_run_tokens transcript primitive (2026-06-30 orchestration-metrics Task 0)."""
from datetime import datetime, timezone
from pathlib import Path

import juggle_run_tokens as rt

FIX = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def test_project_dir_for_cwd():
    assert rt.project_dir_for_cwd("/private/tmp/juggle-juggle-A") == "-private-tmp-juggle-juggle-A"
    assert rt.project_dir_for_cwd("/Users/m/g.x/juggle") == "-Users-m-g-x-juggle"


def test_window_sum_only_inside_record():
    start = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc)
    got = rt.sum_usage_in_window(FIX, start, end)
    assert got == {"input": 200, "output": 20, "cache_read": 7, "cache_write": 2}


def test_agent_cwd_resolves_transcript_not_repo_path(tmp_path):
    """2026-06-30 orchestration-metrics: transcript resolves from agent_cwd (the
    WORKTREE the agent ran in), NOT repo_path (always the main repo). A decoy
    transcript under the main-repo hash dir must be ignored."""
    root = tmp_path / "projects"
    worktree_cwd = "/private/tmp/juggle-juggle-Z"
    main_repo = "/Users/mikechen/github/juggle"
    wt_dir = root / rt.project_dir_for_cwd(worktree_cwd)
    wt_dir.mkdir(parents=True)
    (wt_dir / "sess.jsonl").write_text(FIX.read_text())  # real: 200 in-window
    main_dir = root / rt.project_dir_for_cwd(main_repo)
    main_dir.mkdir(parents=True)
    (main_dir / "sess.jsonl").write_text(
        '{"type":"assistant","timestamp":"2026-06-30T12:05:00.000Z",'
        '"message":{"usage":{"input_tokens":999,"output_tokens":999,'
        '"cache_read_input_tokens":999,"cache_creation_input_tokens":999}}}\n'
    )  # decoy: read only if the bug (repo_path resolution) is present
    run = {"repo_path": main_repo, "agent_cwd": worktree_cwd, "session_id": "sess",
           "dispatched_at": "2026-06-30T12:00:00", "completed_at": "2026-06-30T12:10:00"}
    assert rt.read_run_tokens(run, projects_root=root) == {
        "input": 200, "output": 20, "cache_read": 7, "cache_write": 2}


def test_read_run_tokens_missing_file_is_zeros():
    run = {"repo_path": "/nope", "session_id": "x", "dispatched_at": "2026-06-30T12:00:00",
           "completed_at": "2026-06-30T12:10:00"}
    assert rt.read_run_tokens(run, projects_root=Path("/does/not/exist")) == {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
