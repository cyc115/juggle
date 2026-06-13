"""CLI tests for `juggle runs` (list / show / prune)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

CLI = str(Path(__file__).parent.parent / "src" / "juggle_cli.py")


def run_cli(args, db_path):
    env = os.environ.copy()
    return subprocess.run(
        [sys.executable, CLI] + args,
        capture_output=True, text=True,
        env={**env, "_JUGGLE_TEST_DB": str(db_path)},
    )


@pytest.fixture
def db_with_run(tmp_path):
    db_path = tmp_path / "test.db"
    from juggle_db import JuggleDB

    db = JuggleDB(str(db_path))
    db.init_db()
    tid = db.create_thread("T", session_id="")
    rid = db.insert_agent_run(
        thread_id=tid, input_prompt="THE FULL INPUT PROMPT", agent_id="a",
        role="coder", model="opus", harness="claude", project_id="PX",
        topic_id="TX", task_id="NX",
    )
    db.close_run(tid, output="THE OUTPUT", diffstat="1 file", status="completed")
    return db_path, tid, rid


def test_runs_list_human(db_with_run):
    db_path, _, rid = db_with_run
    r = run_cli(["runs"], db_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert str(rid) in r.stdout
    assert "completed" in r.stdout
    assert "coder" in r.stdout


def test_runs_list_json_and_filter(db_with_run):
    db_path, _, rid = db_with_run
    r = run_cli(["runs", "--project", "PX", "--json"], db_path)
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert len(data) == 1
    assert data[0]["id"] == rid
    assert data[0]["project_id"] == "PX"
    # A non-matching filter returns empty.
    r2 = run_cli(["runs", "--project", "NOPE", "--json"], db_path)
    assert json.loads(r2.stdout) == []


def test_runs_show_full(db_with_run):
    db_path, _, rid = db_with_run
    r = run_cli(["runs", "show", str(rid)], db_path)
    assert r.returncode == 0
    assert "THE FULL INPUT PROMPT" in r.stdout
    assert "THE OUTPUT" in r.stdout
    assert "1 file" in r.stdout


def test_runs_show_missing(db_with_run):
    db_path, _, _ = db_with_run
    r = run_cli(["runs", "show", "999999"], db_path)
    assert r.returncode == 1
    assert "not found" in r.stdout.lower()


def test_runs_prune_accepts_d_suffix(db_with_run):
    db_path, _, _ = db_with_run
    r = run_cli(["runs", "prune", "--older-than", "0d"], db_path)
    assert r.returncode == 0
    assert "Pruned" in r.stdout
