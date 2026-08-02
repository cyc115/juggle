"""Tests for juggle_schedule_dogfood."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import schedules.common as common
import schedules.dogfood as dogfood


def _since(days: int = 7) -> str:
    """The `since_date` run() computes: days_ago_iso(days)[:10]."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Idempotency: prior open dogfood thread blocks run
# ---------------------------------------------------------------------------

def test_prior_dogfood_thread_blocks_run(tmp_path):
    mock_db = MagicMock()

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch("schedules.dogfood_db.db_query",
               return_value=[{"title": "dogfood-2026-05-11"}]), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(common, "STATE_FILE", tmp_path / "state.json"), \
         patch.object(common, "JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=True)
    assert result == 1


# ---------------------------------------------------------------------------
# Dry run produces /tmp artifact
# ---------------------------------------------------------------------------

def test_dry_run_writes_report(tmp_path, monkeypatch):
    mock_db = MagicMock()
    mock_db.add_action_item = MagicMock()

    # M1 (2026-06-21): isolate the dry-run sample to a fresh tmp dir (no stale
    # /tmp false-green).
    monkeypatch.setenv("JUGGLE_SCHEDULE_SAMPLE_DIR", str(tmp_path))
    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch("schedules.dogfood_db.db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=True)

    assert result == 0
    report = tmp_path / "schedule-dogfood-sample-report.md"
    assert report.exists()
    content = report.read_text()
    assert "DRY RUN" in content or "Juggle Self-Analysis" in content


# ---------------------------------------------------------------------------
# Idempotency: running twice doesn't double-file action items
# ---------------------------------------------------------------------------

def test_action_item_filed_once_on_dry_run(tmp_path):
    mock_db = MagicMock()
    add_calls = []
    mock_db.add_action_item = lambda **kw: add_calls.append(kw)

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch("schedules.dogfood_db.db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        # dry_run=True skips action item filing
        dogfood.run(dry_run=True)
        dogfood.run(dry_run=True)

    # dry_run should not call add_action_item
    assert len(add_calls) == 0


# ---------------------------------------------------------------------------
# Cost cap triggers correctly
# ---------------------------------------------------------------------------

def test_cost_cap_aborts_and_writes_partial(tmp_path):
    mock_db = MagicMock()

    def fake_headless(prompt, tracker, dry_run):
        tracker.add(2.0)  # exceeds $1.00 cap
        return "some output"

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch("schedules.dogfood_db.db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "_run_headless_research", fake_headless), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch.object(dogfood, "JUGGLE_REPO", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=False)

    assert result == 1


# ---------------------------------------------------------------------------
# Active session conflict defers
# ---------------------------------------------------------------------------

def test_active_session_defers_once(tmp_path):
    mock_db = MagicMock()
    call_count = [0]

    def fake_active_session(db):
        call_count[0] += 1
        return True  # always active

    with patch.object(dogfood, "get_db", return_value=mock_db), \
         patch("schedules.dogfood_db.db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", fake_active_session), \
         patch("time.sleep", return_value=None), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        result = dogfood.run(dry_run=False)

    assert result == 1
    assert call_count[0] == 2  # checked twice (initial + retry)


# ---------------------------------------------------------------------------
# Live-DB snapshot — the routine must analyze real DB data, not git history
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path, name="snap.db", init=True):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / name))
    if init:
        db.init_db()
    return db


def _seed_window(db, when=None, n_nodes=1):
    """Insert one row per snapshot table, timestamped inside the 7-day window."""
    when = when or datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        for i in range(n_nodes):
            conn.execute(
                "INSERT INTO nodes (id, kind, title, state, created_at, updated_at) "
                "VALUES (?,'conversation',?,'open',?,?)",
                (f"n{i}", f"widget-refactor-{i}", when, when),
            )
        conn.execute(
            "INSERT INTO agent_completions (role, duration_secs, completed_at) "
            "VALUES ('coder', 42.0, ?)", (when,))
        conn.execute(
            "INSERT INTO watchdog_events (agent_id, thread_id, event_type, created_at) "
            "VALUES ('a1', 'n0', 'stalled', ?)", (when,))
        conn.execute(
            "INSERT INTO action_items (thread_id, message, type, priority, created_at) "
            "VALUES ('n0', 'do the thing', 'decision', 'high', ?)", (when,))
        conn.commit()


def test_snapshot_reports_live_rows_from_every_table(tmp_path):
    """The snapshot must surface all four window tables the dogfood prompt names."""
    from schedules.dogfood_db import _extract_db_snapshot

    db = _fresh_db(tmp_path)
    _seed_window(db)
    snap = _extract_db_snapshot(db, _since())

    assert "widget-refactor-0" in snap, f"nodes missing from snapshot:\n{snap}"
    assert "coder" in snap, f"agent_completions missing:\n{snap}"
    assert "stalled" in snap, f"watchdog_events missing:\n{snap}"
    assert "decision" in snap, f"action_items missing:\n{snap}"
    assert len(snap) <= 2000


def test_snapshot_excludes_rows_outside_the_window(tmp_path):
    """Rows older than since_date must not leak into a 7-day snapshot."""
    from schedules.dogfood_db import _extract_db_snapshot

    db = _fresh_db(tmp_path)
    _seed_window(db, when="2020-01-01T00:00:00+00:00")
    snap = _extract_db_snapshot(db, _since())

    assert "widget-refactor-0" not in snap
    assert "No DB data available" in snap


def test_snapshot_on_empty_db_degrades_to_marker(tmp_path):
    """Empty-but-migrated DB: a clear marker, never a crash and never a fake value.

    The routine runs in a cloud container where the DB is often freshly created.
    """
    from schedules.dogfood_db import _extract_db_snapshot

    snap = _extract_db_snapshot(_fresh_db(tmp_path, name="empty.db"), _since())

    assert "No DB data available" in snap


def test_snapshot_on_missing_schema_never_raises(tmp_path):
    """Absent/pre-migration DB (no tables at all) must degrade, not raise."""
    from schedules.dogfood_db import _extract_db_snapshot

    db = _fresh_db(tmp_path, name="nonexistent.db", init=False)

    assert "No DB data available" in _extract_db_snapshot(db, _since())


def test_snapshot_survives_a_partially_migrated_db(tmp_path):
    """One readable table + one missing table => partial snapshot, not total loss.

    Guards the per-section isolation: an older schema costs only the sections it
    cannot serve.
    """
    from schedules.dogfood_db import _extract_db_snapshot

    db = _fresh_db(tmp_path)
    _seed_window(db)
    with db._connect() as conn:
        conn.execute("DROP TABLE watchdog_events")
        conn.commit()

    snap = _extract_db_snapshot(db, _since())
    assert "widget-refactor-0" in snap      # surviving section still rendered
    assert "stalled" not in snap            # dropped section simply absent


def test_snapshot_is_capped(tmp_path):
    """A busy week must not blow the prompt budget."""
    from schedules.dogfood_db import _extract_db_snapshot

    db = _fresh_db(tmp_path)
    _seed_window(db, n_nodes=200)

    assert len(_extract_db_snapshot(db, _since())) <= 2000
    assert len(_extract_db_snapshot(db, _since(), char_cap=200)) <= 200


# ---------------------------------------------------------------------------
# Wiring — the actual 4-week defect
# ---------------------------------------------------------------------------

def test_task_prompt_template_carries_snapshot_between_analysis_and_output():
    """{db_snapshot} sits after the analysis instructions, before the output format."""
    t = dogfood.TASK_PROMPT_TEMPLATE

    assert "{db_snapshot}" in t
    assert t.index("patterns of user friction") < t.index("{db_snapshot}"), \
        "snapshot must come after the analysis instructions"
    assert t.index("{db_snapshot}") < t.index("Output a structured report"), \
        "snapshot must come before the output-format requirement"


def test_run_embeds_db_snapshot_in_the_research_prompt(tmp_path, monkeypatch):
    """dogfood-ran-blind (2026-07-11..2026-08-01): run() built the research prompt
    with no DB access at all, so four consecutive weekly reports were derived from
    git commit messages alone and each named this as its own #1 recommendation.

    Pin: the prompt handed to the research agent carries the live-DB snapshot.
    """
    captured = {}

    def fake_headless(prompt, tracker, dry_run):
        captured["prompt"] = prompt
        return "## Suggested Improvements (1-3)\n1. something\n"

    monkeypatch.setenv("JUGGLE_SCHEDULE_SAMPLE_DIR", str(tmp_path))
    with patch.object(dogfood, "get_db", return_value=MagicMock()), \
         patch("schedules.dogfood_db.db_query", return_value=[]), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "_extract_db_snapshot", return_value="SNAPSHOT-SENTINEL"), \
         patch.object(dogfood, "_run_headless_research", fake_headless), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        dogfood.run(dry_run=True)

    assert "SNAPSHOT-SENTINEL" in captured["prompt"], \
        "run() must embed the live-DB snapshot in the research prompt"


def test_run_survives_a_snapshot_failure(tmp_path, monkeypatch):
    """A snapshot that blows up must never take the weekly routine down with it."""
    monkeypatch.setenv("JUGGLE_SCHEDULE_SAMPLE_DIR", str(tmp_path))
    with patch.object(dogfood, "get_db", return_value=MagicMock()), \
         patch("schedules.dogfood_db.db_query", side_effect=RuntimeError("boom")), \
         patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
         patch.object(dogfood, "_check_active_session", return_value=False), \
         patch.object(dogfood, "_tmux_session_exists", return_value=False), \
         patch.object(dogfood, "REPORTS_DIR", tmp_path), \
         patch("schedules.common.STATE_FILE", tmp_path / "state.json"), \
         patch("schedules.common.JUGGLE_DIR", tmp_path):
        assert dogfood.run(dry_run=True) == 0
