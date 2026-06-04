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
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P1"}'):
        assert infer_project_id("automate investing ideas", PROJECTS) == "P1"


def test_infer_empty_projects_returns_inbox_without_llm_call():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call") as mock:
        result = infer_project_id("some topic", [])
    mock.assert_not_called()
    assert result == "INBOX"


def test_infer_unknown_project_id_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "P99"}'):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"


def test_infer_llm_returns_inbox_sentinel():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "INBOX"}'):
        assert infer_project_id("random topic", PROJECTS) == "INBOX"


def test_infer_llm_returns_none_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=None):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"


def test_infer_invalid_json_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects._cheap_llm_call", return_value="not json at all"):
        assert infer_project_id("some topic", PROJECTS) == "INBOX"


def test_assign_project_background_thread_is_not_daemon(tmp_path):
    """Thread must be non-daemon so process waits for LLM assignment before exiting."""
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    tid = db.create_thread("some topic", session_id="s1")
    with patch("juggle_cmd_projects._cheap_llm_call", return_value='{"project_id": "INBOX"}'):
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
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=f'{{"project_id": "{pid}"}}'):
        t = assign_project_background(db, tid, "automate investing ideas", _return_thread=True)
        t.join(timeout=5)
    assert db.get_thread(tid)["project_id"] == pid


def test_assign_project_background_silent_on_llm_failure(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import assign_project_background
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    tid = db.create_thread("some topic", session_id="s1")
    with patch("juggle_cmd_projects._cheap_llm_call", side_effect=Exception("network error")):
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
    with patch("juggle_cmd_projects._cheap_llm_call", return_value=f'{{"project_id": "{pid}"}}'):
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
    original_cheap_llm = None

    def capturing_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return f'{{"project_id": "{pid}"}}'

    with patch("juggle_cmd_projects._cheap_llm_call", side_effect=capturing_llm):
        infer_project_id("some trading topic", projects, db=db)

    assert "sell TSLA puts" in captured["prompt"]
    assert "buy AAPL calls" in captured["prompt"]
