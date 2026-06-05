import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_build_match_profile_prompt_contains_project_name():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI assistant platform"}
    threads = [
        {"topic": "fix agent dispatch bug", "assigned_by": "human"},
        {"topic": "add terraform module", "assigned_by": "human"},
        {"topic": "auto-assigned CI thread", "assigned_by": "auto"},
    ]
    corrections = [{"topic": "investing script", "from_project": "P1", "to_project": "P2"}]
    prompt = build_match_profile_prompt(project, threads, corrections)
    assert "LifeOS Dev" in prompt
    assert "fix agent dispatch bug" in prompt
    assert "auto-assigned CI thread" in prompt


def test_build_match_profile_prompt_human_weighted_before_auto():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [
        {"topic": "human thread", "assigned_by": "human"},
        {"topic": "auto thread", "assigned_by": "auto"},
    ]
    prompt = build_match_profile_prompt(project, threads, [])
    assert "human thread" in prompt


def test_build_match_profile_prompt_includes_negative_framing():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    prompt = build_match_profile_prompt(project, [], [])
    assert "NOT" in prompt or "negative" in prompt.lower() or "sibling" in prompt.lower()


from unittest.mock import patch, MagicMock


def test_synth_project_writes_match_profile(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build things")
    tid = db.create_thread("software task", session_id="s1")
    db.update_thread(tid, project_id=pid, assigned_by="human")
    with patch("juggle_cmd_projects.llm_call", return_value=(
        "Software development threads.\nKEYWORDS: code, deploy, CI\nNOT: finance, investing"
    )):
        synth_project(db, pid)
    p = db.get_project(pid)
    assert "Software development" in p["match_profile"]
    assert p["profile_dirty"] == 0
    assert p["profile_synth_at"] is not None


def test_synth_project_skips_if_no_threads_and_no_force(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Empty", "No threads yet")
    with patch("juggle_cmd_projects.llm_call") as mock_llm:
        synth_project(db, pid)
    mock_llm.assert_not_called()


def test_assign_marks_old_project_dirty(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import _assign_thread_to_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    p1 = db.create_project("P1", "obj1")
    p2 = db.create_project("P2", "obj2")
    tid = db.create_thread("some topic", session_id="s1")
    db.update_thread(tid, project_id=p1, assigned_by="human")
    _assign_thread_to_project(db, tid, p2, assigned_by="human")
    assert db.get_project(p1)["profile_dirty"] == 1


def test_build_match_profile_prompt_bounded_thread_count():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [{"topic": f"thread {i}", "assigned_by": "human"} for i in range(50)]
    prompt = build_match_profile_prompt(project, threads, [])
    assert prompt.count("thread ") <= 32


# ---------------------------------------------------------------------------
# Phase 3: drift_score
# ---------------------------------------------------------------------------

def test_drift_score_identical_vectors_is_zero():
    from juggle_cmd_projects import drift_score
    v = [1.0, 0.5, 0.3]
    assert drift_score(v, v) == pytest.approx(0.0, abs=1e-6)


def test_drift_score_orthogonal_is_one():
    from juggle_cmd_projects import drift_score
    assert drift_score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0, abs=1e-6)


def test_drift_score_handles_zero_vector():
    from juggle_cmd_projects import drift_score
    assert drift_score([0.0, 0.0], [1.0, 0.0]) == 1.0


# ---------------------------------------------------------------------------
# Phase 3: check_and_resynth_if_drifted
# ---------------------------------------------------------------------------

def test_check_and_resynth_triggers_on_drift(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import check_and_resynth_if_drifted
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build software")
    for i in range(5):
        tid = db.create_thread(f"software task {i}", session_id="s1")
        db.update_thread(tid, project_id=pid, assigned_by="human")
    db.set_match_profile(pid, "Software dev. KEYWORDS: code, deploy. NOT: finance")
    with patch("juggle_cmd_projects.synth_project") as mock_synth:
        with patch("juggle_cmd_projects.drift_score", return_value=0.9):
            check_and_resynth_if_drifted(db, pid, threshold=0.5)
    mock_synth.assert_called_once_with(db, pid)


def test_check_and_resynth_skips_below_threshold(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import check_and_resynth_if_drifted
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    tid = db.create_thread("task", session_id="s1")
    db.update_thread(tid, project_id=pid, assigned_by="human")
    db.set_match_profile(pid, "Software dev. KEYWORDS: code. NOT: finance")
    # Add more threads so we have >= 3
    for i in range(3):
        t = db.create_thread(f"extra {i}", session_id="s1")
        db.update_thread(t, project_id=pid, assigned_by="human")
    with patch("juggle_cmd_projects.synth_project") as mock_synth:
        with patch("juggle_cmd_projects.drift_score", return_value=0.1):
            check_and_resynth_if_drifted(db, pid, threshold=0.5)
    mock_synth.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 4: resweep_inbox
# ---------------------------------------------------------------------------

def test_resweep_inbox_reclassifies_unassigned(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import resweep_inbox
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build software")
    tid = db.create_thread("software task", session_id="s1")
    assert db.get_thread(tid)["project_id"] == "INBOX"
    with patch("juggle_cmd_projects.infer_project_id", return_value=(pid, 0.85)):
        resweep_inbox(db, limit=10)
    assert db.get_thread(tid)["project_id"] == pid


def test_resweep_inbox_respects_limit(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import resweep_inbox
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.create_project("Dev", "Build software")
    for i in range(10):
        db.create_thread(f"task {i}", session_id="s1")
    call_count = 0

    def fake_infer(topic, projects, db=None, **kw):
        nonlocal call_count
        call_count += 1
        return ("INBOX", 0.3)

    with patch("juggle_cmd_projects.infer_project_id", side_effect=fake_infer):
        resweep_inbox(db, limit=5)
    assert call_count == 5
