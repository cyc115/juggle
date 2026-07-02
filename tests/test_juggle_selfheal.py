"""Tests for juggle_selfheal — self-healing error capture pipeline."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _non_agent_context(monkeypatch, tmp_path):
    """These record_error pins assert the DIRECT-WRITE (non-agent) path. Neutralize
    the ambient agent context (this suite may run inside a dispatched agent / a
    juggle-juggle-* worktree) so record_error does not divert to the spool
    (T-spool-06); agent-context spooling is covered by test_spool_record_error.py."""
    for var in ("JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_AGENT_WORKTREE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Task 1 gate: Class B transcript parsing (real schema)
# Stays RED until Task 6 implements _do_class_b_scan in juggle_hooks.py
# ---------------------------------------------------------------------------

def test_class_b_scan_real_schema_detects_juggle_tool_error():
    """_do_class_b_scan finds a tool error attributed to juggle: skill invocation."""
    from juggle_hooks import _do_class_b_scan

    transcript = FIXTURES_DIR / "transcript_class_b.jsonl"
    assert transcript.exists(), f"Fixture missing: {transcript}"

    calls: list = []

    def fake_record(tool, tool_input, error_text, juggle_ref):
        calls.append({"tool": tool, "error_text": error_text, "juggle_ref": juggle_ref})

    with patch("juggle_selfheal.record_orchestration_error", side_effect=fake_record):
        _do_class_b_scan(transcript)

    assert len(calls) == 1, f"Expected 1 call, got {calls}"
    assert calls[0]["tool"] == "Skill"
    assert "InputValidationError" in calls[0]["error_text"]
    assert "juggle:" in calls[0]["juggle_ref"]


def test_class_b_scan_no_error_when_is_error_false(tmp_path):
    """_do_class_b_scan ignores tool_result where is_error is False."""
    from juggle_hooks import _do_class_b_scan

    transcript = tmp_path / "ok.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"start juggle"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_002","name":"Skill","input":{"skill":"juggle:start"},"caller":"user"}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_002","is_error":false,"content":"OK"}]}}\n'
    )
    calls: list = []
    with patch("juggle_selfheal.record_orchestration_error", side_effect=lambda *a, **kw: calls.append(a)):
        _do_class_b_scan(transcript)
    assert calls == [], "Should not record when is_error is False"


def test_class_b_scan_no_attribution_without_juggle_ref(tmp_path):
    """_do_class_b_scan does not record errors unrelated to juggle."""
    from juggle_hooks import _do_class_b_scan

    transcript = tmp_path / "unrelated.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"do something"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_003","name":"Bash","input":{"command":"ls /nonexistent"},"caller":"user"}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_003","is_error":true,"content":"No such file"}]}}\n'
    )
    calls: list = []
    with patch("juggle_selfheal.record_orchestration_error", side_effect=lambda *a, **kw: calls.append(a)):
        _do_class_b_scan(transcript)
    assert calls == [], "Should not record when no juggle ref in tool inputs"


def test_class_b_scan_missing_transcript_path_is_silent():
    """_scan_transcript_for_class_b silently skips when transcript_path absent."""
    from juggle_hooks import _scan_transcript_for_class_b

    _scan_transcript_for_class_b({})
    _scan_transcript_for_class_b({"transcript_path": None})


# ---------------------------------------------------------------------------
# Task 2: error_events table + DB helpers
# ---------------------------------------------------------------------------

def test_error_events_table_created_by_init_db(tmp_path):
    """init_db() creates error_events table with expected columns."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(error_events)").fetchall()}

    expected = {
        "id", "signature_hash", "error_class", "exc_type", "traceback",
        "entrypoint", "surface", "command_args", "juggle_ref",
        "count", "first_seen", "last_seen", "status", "action_item_id",
    }
    assert expected <= cols, f"Missing columns: {expected - cols}"


def test_error_events_unique_index_on_sig(tmp_path):
    """signature_hash has a UNIQUE index."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with db._connect() as conn:
        indexes = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='error_events'"
            ).fetchall()
        ]
    assert "idx_error_events_sig" in indexes


def test_dedup_or_insert_inserts_new_row(tmp_path):
    """dedup_or_insert_error inserts a new row for an unseen signature."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    row_id = db.dedup_or_insert_error(
        signature_hash="abc123",
        error_class="A",
        exc_type="KeyError",
        traceback="Traceback...",
        entrypoint="juggle_cli.main",
        command_args='["juggle", "start"]',
    )
    assert row_id is not None

    with db._connect() as conn:
        row = conn.execute("SELECT * FROM error_events WHERE id = ?", (row_id,)).fetchone()
    assert row["signature_hash"] == "abc123"
    assert row["status"] == "open"
    assert row["count"] == 1


def test_dedup_or_insert_increments_count_on_dup(tmp_path):
    """dedup_or_insert_error increments count for existing non-resolved signature."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    db.dedup_or_insert_error("sig1", "A", "ValueError", "tb", "cli", "[]")
    result = db.dedup_or_insert_error("sig1", "A", "ValueError", "tb2", "cli", "[]")
    assert result is None  # suppressed — dedup, no new row

    with db._connect() as conn:
        row = conn.execute("SELECT count FROM error_events WHERE signature_hash = 'sig1'").fetchone()
    assert row["count"] == 2


def test_dedup_allows_insert_after_resolved(tmp_path):
    """dedup_or_insert_error inserts fresh row if prior row is resolved (regression)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    first_id = db.dedup_or_insert_error("sig2", "A", "KeyError", "tb", "cli", "[]")
    db.set_error_event_status(first_id, "resolved")
    second_id = db.dedup_or_insert_error("sig2", "A", "KeyError", "tb", "cli", "[]")
    assert second_id is not None
    assert second_id != first_id


def test_set_error_event_status_updates_row(tmp_path):
    """set_error_event_status updates status (and optionally action_item_id)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig3", "A", "RuntimeError", "tb", "cli", "[]")

    db.set_error_event_status(row_id, "diagnosing")
    with db._connect() as conn:
        row = conn.execute("SELECT status, action_item_id FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"
    assert row["action_item_id"] is None

    db.set_error_event_status(row_id, "awaiting_approval", action_item_id=42)
    with db._connect() as conn:
        row = conn.execute("SELECT status, action_item_id FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "awaiting_approval"
    assert row["action_item_id"] == 42


def test_get_pending_selfheal_count(tmp_path):
    """get_pending_selfheal_count returns count of non-resolved rows."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    db.dedup_or_insert_error("s1", "A", "E1", "tb", "cli", "[]")
    id2 = db.dedup_or_insert_error("s2", "A", "E2", "tb", "cli", "[]")
    db.dedup_or_insert_error("s3", "A", "E3", "tb", "cli", "[]")
    db.set_error_event_status(id2, "resolved")

    assert db.get_pending_selfheal_count() == 2  # s1 and s3


# ---------------------------------------------------------------------------
# Task 3: CLI helpers (selfheal subcommands)
# ---------------------------------------------------------------------------

def test_list_selfheal_prints_open_rows(tmp_path, capsys):
    """list-selfheal prints one line per non-resolved error_events row."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_list_selfheal

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.dedup_or_insert_error("aabbccdd11223344", "A", "KeyError", "tb", "juggle_cli.main", "[]")
    db.dedup_or_insert_error("ff00ee11dd223344", "B", None, "Tool err", "Monitor", "[]",
                              juggle_ref="commands/start.md")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")

    _cmd_list_selfheal(FakeArgs())
    out = capsys.readouterr().out
    assert "[A]" in out
    assert "[B]" in out
    assert "KeyError" in out
    assert "Monitor" in out


def test_selfheal_set_status_updates_row(tmp_path, capsys):
    """selfheal-set-status updates status and prints confirmation."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_selfheal_set_status

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig-test", "A", "KeyError", "tb", "cli", "[]")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id
        status = "diagnosing"
        action_item_id = None

    _cmd_selfheal_set_status(FakeArgs())
    out = capsys.readouterr().out
    assert "diagnosing" in out

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"


def test_selfheal_reset_diagnosing(tmp_path, capsys):
    """selfheal-reset-diagnosing resets diagnosing→open."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_selfheal_reset_diagnosing

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig-reset", "A", "KeyError", "tb", "cli", "[]")
    db.set_error_event_status(row_id, "diagnosing")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id

    _cmd_selfheal_reset_diagnosing(FakeArgs())
    out = capsys.readouterr().out
    assert "open" in out

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "open"


# ---------------------------------------------------------------------------
# show-selfheal <id>: single-entry triage detail (command_args + traceback +
# status + counts). Complements list-selfheal which only prints summary lines.
# ---------------------------------------------------------------------------

def test_get_error_event_returns_full_row(tmp_path):
    """get_error_event(id) returns the full row dict; None for a missing id."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error(
        "sig-detail", "A", "RuntimeError", "Traceback... boom",
        "send_task", "['cockpit', '--out']",
    )

    row = db.get_error_event(row_id)
    assert row is not None
    assert row["id"] == row_id
    assert row["exc_type"] == "RuntimeError"
    assert row["traceback"] == "Traceback... boom"
    assert row["command_args"] == "['cockpit', '--out']"
    assert row["status"] == "open"
    assert row["count"] == 1

    assert db.get_error_event(999999) is None


def test_show_selfheal_human_prints_full_detail(tmp_path, capsys):
    """show-selfheal <id> prints command_args, traceback, status, and counts."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_show_selfheal

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error(
        "20f69693aabbccdd", "A", "RuntimeError",
        "RuntimeError: send_task: submission not verified for pane after retries",
        "send_task", "['send-task', 'pane-7']",
    )

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id
        json = False

    _cmd_show_selfheal(FakeArgs())
    out = capsys.readouterr().out
    assert str(row_id) in out
    assert "RuntimeError" in out
    assert "submission not verified" in out          # traceback
    assert "['send-task', 'pane-7']" in out          # command_args
    assert "open" in out                             # status
    assert "count" in out.lower()                    # counts


def test_show_selfheal_json_emits_full_row(tmp_path, capsys):
    """show-selfheal --id <id> --json emits the full row as a JSON object."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_show_selfheal

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error(
        "sig-json", "B", None, "Tool err detail", "Monitor", "['x']",
        juggle_ref="commands/start.md",
    )

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id
        json = True

    _cmd_show_selfheal(FakeArgs())
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["id"] == row_id
    assert payload["error_class"] == "B"
    assert payload["traceback"] == "Tool err detail"
    assert payload["command_args"] == "['x']"


def test_show_selfheal_missing_id_exits_nonzero(tmp_path, capsys):
    """show-selfheal on an unknown id prints an error and exits non-zero."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_show_selfheal

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = 424242
        json = False

    with pytest.raises(SystemExit):
        _cmd_show_selfheal(FakeArgs())
    out = capsys.readouterr().out
    assert "not found" in out


# ---------------------------------------------------------------------------
# Task 4: juggle_selfheal core — signature, allowlist, record_error
# ---------------------------------------------------------------------------

def test_allowlist_systemexit():
    """record_error is a no-op for SystemExit."""
    from juggle_selfheal import record_error
    os.environ.pop("JUGGLE_SELFHEAL_OP", None)

    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(SystemExit(1), "test.entrypoint")
        mock_db.assert_not_called()


def test_allowlist_keyboardinterrupt():
    """record_error is a no-op for KeyboardInterrupt."""
    from juggle_selfheal import record_error
    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(KeyboardInterrupt(), "test.entrypoint")
        mock_db.assert_not_called()


def test_allowlist_sqlite_locked():
    """record_error is a no-op for sqlite 'database is locked' errors."""
    import sqlite3
    from juggle_selfheal import record_error
    exc = sqlite3.OperationalError("sqlite database is locked")
    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(exc, "test.entrypoint")
        mock_db.assert_not_called()


def test_self_protection_env_var():
    """record_error is a no-op when JUGGLE_SELFHEAL_OP env var is set."""
    from juggle_selfheal import record_error
    os.environ["JUGGLE_SELFHEAL_OP"] = "1"
    try:
        with patch("juggle_selfheal._get_db") as mock_db:
            record_error(RuntimeError("test"), "test.entrypoint")
            mock_db.assert_not_called()
    finally:
        del os.environ["JUGGLE_SELFHEAL_OP"]


def test_signature_dedup_class_a(tmp_path):
    """Two identical exceptions produce same signature_hash; second call increments count."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def fake_exc():
        raise KeyError("thread_id")

    exc1 = exc2 = None
    try:
        fake_exc()
    except KeyError as e:
        exc1 = e
    try:
        fake_exc()
    except KeyError as e:
        exc2 = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc1, "juggle_cli.main")
        record_error(exc2, "juggle_cli.main")

    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM error_events").fetchall()
    assert len(rows) == 1, f"Expected 1 row (dedup), got {len(rows)}"
    assert rows[0]["count"] == 2


def test_signature_different_exc_type(tmp_path):
    """Different exception types produce different signatures."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def raise_key():
        raise KeyError("k")

    def raise_value():
        raise ValueError("v")

    exc_k = exc_v = None
    try:
        raise_key()
    except KeyError as e:
        exc_k = e
    try:
        raise_value()
    except ValueError as e:
        exc_v = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc_k, "juggle_cli.main")
        record_error(exc_v, "juggle_cli.main")

    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM error_events").fetchall()
    assert len(rows) == 2, "Different exc types must produce different signatures"


def test_resolved_regression(tmp_path):
    """Same signature after resolved → new INSERT (regression path)."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def raise_key():
        raise KeyError("x")

    exc = None
    try:
        raise_key()
    except KeyError as e:
        exc = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    with db._connect() as conn:
        row = conn.execute("SELECT id FROM error_events").fetchone()
    db.set_error_event_status(row["id"], "resolved")

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    with db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
    assert count == 2, "Resolved regression should insert a fresh row"


def test_concurrency_cap_in_flight(tmp_path):
    """_try_claim_diagnosis_slot returns False when another row is already diagnosing."""
    from juggle_db import JuggleDB
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    id1 = db.dedup_or_insert_error("s-diag", "A", "E1", "tb", "cli", "[]")
    id2 = db.dedup_or_insert_error("s-open", "A", "E2", "tb", "cli", "[]")

    db.set_error_event_status(id1, "diagnosing")

    result = _try_claim_diagnosis_slot(db, id2)
    assert result is False


def test_concurrency_cap_single_claim(tmp_path):
    """_try_claim_diagnosis_slot returns True when no other row is diagnosing."""
    from juggle_db import JuggleDB
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    row_id = db.dedup_or_insert_error("s-single", "A", "E", "tb", "cli", "[]")
    result = _try_claim_diagnosis_slot(db, row_id)
    assert result is True

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"


def test_record_orchestration_error_class_b(tmp_path):
    """record_orchestration_error inserts a Class B row."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_orchestration_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with patch("juggle_selfheal._get_db", return_value=db):
        record_orchestration_error(
            tool="Monitor",
            tool_input={"command": "scripts/juggle-agent-monitor"},
            error_text="InputValidationError: 'command' is a required property",
            juggle_ref="juggle:",
        )

    with db._connect() as conn:
        row = conn.execute("SELECT * FROM error_events").fetchone()
    assert row is not None
    assert row["error_class"] == "B"
    assert row["entrypoint"] == "Monitor"
    assert row["status"] == "open"


# ---------------------------------------------------------------------------
# Task 8: SessionStart pending count
# ---------------------------------------------------------------------------

def test_session_start_includes_selfheal_count(tmp_path):
    """handle_session_start output includes pending self-heal count when > 0."""
    from juggle_db import JuggleDB
    from juggle_hooks import handle_session_start

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path)
    db.init_db()
    db.set_active(True)
    db.dedup_or_insert_error("s1", "A", "KeyError", "tb", "cli", "[]")
    db.dedup_or_insert_error("s2", "A", "ValueError", "tb", "cli", "[]")

    output_lines = []

    def fake_print(s):
        output_lines.append(s)

    with patch("juggle_hooks.DB_PATH", Path(db_path)), \
         patch("juggle_hooks_config.DB_PATH", Path(db_path)), \
         patch("builtins.print", side_effect=fake_print), \
         patch("sys.exit"):
        handle_session_start({"reason": "resume"})

    combined = " ".join(output_lines)
    found = False
    for line in output_lines:
        try:
            parsed = json.loads(line)
            ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
            if "self-heal" in ctx or "selfheal" in ctx.lower() or "pending" in ctx:
                found = True
                break
        except Exception:
            if "self-heal" in line or "pending" in line.lower():
                found = True
                break
    assert found, f"Expected selfheal warning in output, got: {output_lines}"


# ---------------------------------------------------------------------------
# Task 10: Integration tests
# ---------------------------------------------------------------------------

def test_class_a_e2e_record_and_claim(tmp_path):
    """Class A: simulate exception → record → claim diagnosis → resolve → regression."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error, _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def faulty():
        raise RuntimeError("simulated juggle bug")

    exc = None
    try:
        faulty()
    except RuntimeError as e:
        exc = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    rows = db.get_open_error_events()
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["error_class"] == "A"
    assert rows[0]["exc_type"] == "RuntimeError"
    row_id = rows[0]["id"]

    assert _try_claim_diagnosis_slot(db, row_id) is True
    assert db.get_open_error_events()[0]["status"] == "diagnosing"

    db.set_error_event_status(row_id, "awaiting_approval", action_item_id=99)
    assert db.get_open_error_events()[0]["action_item_id"] == 99

    db.set_error_event_status(row_id, "resolved")
    assert db.get_pending_selfheal_count() == 0

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")
    assert db.get_pending_selfheal_count() == 1


def test_class_b_e2e_fixture_to_db(tmp_path):
    """Class B: scan real-schema fixture → DB row → dedup → cap → resolve."""
    from juggle_db import JuggleDB
    from juggle_hooks import _do_class_b_scan
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    transcript = FIXTURES_DIR / "transcript_class_b.jsonl"
    assert transcript.exists()

    with patch("juggle_selfheal._get_db", return_value=db):
        _do_class_b_scan(transcript)

    rows = db.get_open_error_events()
    assert len(rows) == 1
    row = rows[0]
    assert row["error_class"] == "B"
    assert row["entrypoint"] == "Skill"
    assert "juggle:" in row["juggle_ref"]
    assert row["status"] == "open"
    row_id = row["id"]

    with patch("juggle_selfheal._get_db", return_value=db):
        _do_class_b_scan(transcript)
    rows = db.get_open_error_events()
    assert len(rows) == 1
    assert rows[0]["count"] == 2

    sig1 = rows[0]["signature_hash"]
    assert len(sig1) == 16

    assert _try_claim_diagnosis_slot(db, row_id) is True
    db.set_error_event_status(row_id, "resolved")
    assert db.get_pending_selfheal_count() == 0
