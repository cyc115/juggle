import sys
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

PROJECTS = [
    {"id": "P1", "name": "Investing Automation", "objective": "Automate stock idea generation"},
    {"id": "P2", "name": "LifeOS Dev", "objective": "Build AI assistant platform"},
]


def test_infer_exact_match():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value='{"project_id": "P1", "confidence": 0.9}'):
        pid, conf = infer_project_id("automate investing ideas", PROJECTS)
    assert pid == "P1"
    assert conf == pytest.approx(0.9)


def test_infer_empty_projects_returns_inbox_without_llm_call():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call") as mock:
        result = infer_project_id("some topic", [])
    mock.assert_not_called()
    assert result == ("INBOX", 0.0)


def test_infer_unknown_project_id_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value='{"project_id": "P99", "confidence": 0.8}'):
        pid, _ = infer_project_id("some topic", PROJECTS)
    assert pid == "INBOX"


def test_infer_llm_returns_inbox_sentinel():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value='{"project_id": "INBOX", "confidence": 0.7}'):
        pid, _ = infer_project_id("random topic", PROJECTS)
    assert pid == "INBOX"


def test_infer_llm_returns_none_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value=None):
        result = infer_project_id("some topic", PROJECTS)
    assert result == ("INBOX", 0.0)


def test_infer_invalid_json_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value="not json at all"):
        pid, _ = infer_project_id("some topic", PROJECTS)
    assert pid == "INBOX"


def test_infer_low_confidence_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call",
               return_value='{"project_id": "P1", "confidence": 0.3}'):
        pid, conf = infer_project_id("ambiguous topic", PROJECTS)
    assert pid == "INBOX"
    assert conf == pytest.approx(0.3)


def test_infer_high_confidence_returns_project():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call",
               return_value='{"project_id": "P1", "confidence": 0.9}'):
        pid, conf = infer_project_id("automate investing ideas", PROJECTS)
    assert pid == "P1"
    assert conf == pytest.approx(0.9)


def test_infer_returns_tuple():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value=None):
        result = infer_project_id("some topic", PROJECTS)
    assert isinstance(result, tuple)
    assert result == ("INBOX", 0.0)


def test_assign_project_background_thread_is_not_daemon(tmp_path):
    """Thread must be non-daemon so process waits for LLM assignment before exiting."""
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    tid = db.create_thread("some topic", session_id="s1")
    with patch("juggle_cmd_projects.llm_call", return_value='{"project_id": "INBOX", "confidence": 0.5}'):
        t = assign_project_background(db, tid, "some topic", _return_thread=True)
        assert t.daemon is False
        t.join(timeout=5)


def test_assign_project_background_updates_db(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Investing", objective="Automate stock ideas")
    tid = db.create_thread("automate investing ideas", session_id="s1")
    assert db.get_thread(tid)["project_id"] == "INBOX"
    with patch("juggle_cmd_projects.llm_call", return_value=f'{{"project_id": "{pid}", "confidence": 0.9}}'):
        t = assign_project_background(db, tid, "automate investing ideas", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["project_id"] == pid


def test_assign_project_background_silent_on_llm_failure(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    tid = db.create_thread("some topic", session_id="s1")
    with patch("juggle_cmd_projects.llm_call", side_effect=Exception("network error")):
        t = assign_project_background(db, tid, "some topic", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["project_id"] == "INBOX"


# --- Task 2: correction hook + assigned_by ---

def make_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    return db


def test_cmd_project_assign_logs_correction_when_project_changes(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Work", objective="work stuff")
    tid_uuid = db.create_thread("topic alpha", session_id="s1")
    thread = db.get_thread(tid_uuid)
    label = thread["user_label"]

    args = type("Args", (), {"thread_id": label, "project_id": pid})()
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    corrections = db.get_recent_corrections(limit=5)
    assert len(corrections) == 1
    assert corrections[0]["from_project"] == "INBOX"
    assert corrections[0]["to_project"] == pid
    assert corrections[0]["topic"] == "topic alpha"


def test_cmd_project_assign_sets_assigned_by_human(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Work", objective="work stuff")
    tid_uuid = db.create_thread("topic beta", session_id="s1")
    thread = db.get_thread(tid_uuid)
    label = thread["user_label"]

    args = type("Args", (), {"thread_id": label, "project_id": pid})()
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    assert db.get_thread(tid_uuid)["assigned_by"] == "human"


def test_cmd_project_assign_no_correction_when_same_project(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    tid_uuid = db.create_thread("topic gamma", session_id="s1")
    thread = db.get_thread(tid_uuid)
    label = thread["user_label"]

    # Assign to INBOX (same as current)
    args = type("Args", (), {"thread_id": label, "project_id": "INBOX"})()
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(args)

    assert db.get_recent_corrections(limit=5) == []


def test_assign_project_background_sets_assigned_by_auto(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Investing", objective="Automate ideas")
    tid = db.create_thread("automate investing ideas", session_id="s1")
    with patch("juggle_cmd_projects.llm_call", return_value=f'{{"project_id": "{pid}", "confidence": 0.9}}'):
        t = assign_project_background(db, tid, "automate investing ideas", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["assigned_by"] == "auto"


# --- Task 3: _build_classifier_prompt ---

def test_build_classifier_prompt_contains_topic():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [{"id": "P1", "name": "Investing", "objective": "Automate ideas"}]
    prompt = _build_classifier_prompt("buy AAPL options", projects, {}, [])
    assert "buy AAPL options" in prompt


def test_build_classifier_prompt_contains_project_ids():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [
        {"id": "P1", "name": "Investing", "objective": "Automate ideas"},
        {"id": "P2", "name": "LifeOS", "objective": "Build assistant"},
    ]
    prompt = _build_classifier_prompt("some topic", projects, {}, [])
    assert "P1" in prompt
    assert "P2" in prompt


def test_build_classifier_prompt_includes_human_positives():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [{"id": "P1", "name": "Investing", "objective": "Automate ideas"}]
    positives = {"P1": [{"topic": "buy AAPL"}, {"topic": "sell TSLA"}]}
    prompt = _build_classifier_prompt("some topic", projects, positives, [])
    assert "buy AAPL" in prompt
    assert "sell TSLA" in prompt


def test_build_classifier_prompt_includes_corrections():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [{"id": "P1", "name": "Investing", "objective": "Automate ideas"}]
    corrections = [{"topic": "options trading", "from_project": "INBOX", "to_project": "P1"}]
    prompt = _build_classifier_prompt("some topic", projects, {}, corrections)
    assert "options trading" in prompt


def test_build_classifier_prompt_includes_match_profile():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [
        {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI platform",
         "match_profile": "Codebase work: agent dispatch, Terraform, CI. NOT: finance."},
    ]
    prompt = _build_classifier_prompt("fix terraform deploy", projects, {}, [])
    assert "Codebase work" in prompt


def test_build_classifier_prompt_no_match_profile_unchanged():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [
        {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI platform"},
    ]
    prompt = _build_classifier_prompt("fix terraform deploy", projects, {}, [])
    assert "P1" in prompt
    assert "LifeOS Dev" in prompt


def test_infer_project_id_uses_human_positives_and_corrections(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import infer_project_id
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Investing", objective="Automate ideas")
    # Add a human-assigned thread
    tid = db.create_thread("sell TSLA puts", session_id="s1")
    db.update_thread(tid, project_id=pid, assigned_by="human")
    # Add a correction
    db.log_project_correction("buy AAPL calls", from_project="INBOX", to_project=pid)

    projects = db.get_active_projects()
    captured = {}

    def capturing_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return f'{{"project_id": "{pid}", "confidence": 0.9}}'

    with patch("juggle_cmd_projects.llm_call", side_effect=capturing_llm):
        infer_project_id("some trading topic", projects, db=db)

    assert "sell TSLA puts" in captured["prompt"]
    assert "buy AAPL calls" in captured["prompt"]


# ---------------------------------------------------------------------------
# cmd_project_edit — success_criteria
# ---------------------------------------------------------------------------

def _make_project_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project(name="Alpha", objective="Do stuff")
    return db, pid


def _edit_args(**kwargs):
    defaults = {
        "project_id": None,
        "name": None,
        "objective": None,
        "out_of_scope": None,
        "success_criterion": None,
        "success_criteria_json": None,
        "clear_success_criteria": False,
    }
    defaults.update(kwargs)
    return type("Args", (), defaults)()


def test_project_edit_success_criterion_repeatable_sets_list(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(
        project_id=pid,
        success_criterion=["Pipeline ingests all sources", "Daily digest produced"],
    )
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_edit(args)
    import json
    stored = json.loads(db.get_project(pid)["success_criteria"])
    assert stored == ["Pipeline ingests all sources", "Daily digest produced"]


def test_project_edit_success_criteria_json_valid(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(project_id=pid, success_criteria_json='["a", "b"]')
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_edit(args)
    import json
    stored = json.loads(db.get_project(pid)["success_criteria"])
    assert stored == ["a", "b"]


def test_project_edit_success_criteria_json_invalid_json_errors(tmp_path, capsys):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(project_id=pid, success_criteria_json="not valid json")
    with patch("juggle_cmd_projects.get_db", return_value=db):
        with pytest.raises(SystemExit) as exc:
            cmd_project_edit(args)
    assert exc.value.code == 1
    # No DB write — success_criteria unchanged
    import json
    stored = json.loads(db.get_project(pid)["success_criteria"])
    assert stored == []


def test_project_edit_success_criteria_json_non_list_errors(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(project_id=pid, success_criteria_json='{"key": "val"}')
    with patch("juggle_cmd_projects.get_db", return_value=db):
        with pytest.raises(SystemExit) as exc:
            cmd_project_edit(args)
    assert exc.value.code == 1


def test_project_edit_success_criteria_json_non_string_items_errors(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(project_id=pid, success_criteria_json='[1, 2]')
    with patch("juggle_cmd_projects.get_db", return_value=db):
        with pytest.raises(SystemExit) as exc:
            cmd_project_edit(args)
    assert exc.value.code == 1


def test_project_edit_clear_success_criteria(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    import json
    db, pid = _make_project_db(tmp_path)
    db.update_project(pid, success_criteria=json.dumps(["existing criterion"]))
    args = _edit_args(project_id=pid, clear_success_criteria=True)
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_edit(args)
    stored = json.loads(db.get_project(pid)["success_criteria"])
    assert stored == []


def test_project_edit_both_criterion_and_json_errors(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(
        project_id=pid,
        success_criterion=["a"],
        success_criteria_json='["b"]',
    )
    with patch("juggle_cmd_projects.get_db", return_value=db):
        with pytest.raises(SystemExit) as exc:
            cmd_project_edit(args)
    assert exc.value.code == 1


def test_project_edit_existing_flags_regression(tmp_path):
    from juggle_cmd_projects import cmd_project_edit
    db, pid = _make_project_db(tmp_path)
    args = _edit_args(
        project_id=pid,
        name="New Name",
        objective="New Objective",
        out_of_scope="Out of scope text",
    )
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_edit(args)
    p = db.get_project(pid)
    assert p["name"] == "New Name"
    assert p["objective"] == "New Objective"
    assert p["out_of_scope"] == "Out of scope text"


# ---------------------------------------------------------------------------
# Phase 5: bulk + archived assign
# ---------------------------------------------------------------------------

import types


def _make_args(**kw):
    ns = types.SimpleNamespace()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_cmd_project_assign_archived_thread(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    tid = db.create_thread("archived task", session_id="s1")
    db.archive_thread(tid)
    t = db.get_thread(tid)
    label = t["user_label"]
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(_make_args(thread_id=[label], project_id=pid))
    assert db.get_thread(tid)["project_id"] == pid


def test_cmd_project_assign_bulk(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    t1 = db.create_thread("task one", session_id="s1")
    t2 = db.create_thread("task two", session_id="s1")
    l1 = db.get_thread(t1)["user_label"]
    l2 = db.get_thread(t2)["user_label"]
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(_make_args(thread_id=[l1, l2, pid], project_id=None))
    assert db.get_thread(t1)["project_id"] == pid
    assert db.get_thread(t2)["project_id"] == pid
