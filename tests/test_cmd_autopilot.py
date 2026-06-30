"""Tests for `juggle autopilot` — global on/off toggle only (P7: arming removed).

P7: arm/disarm subcommands are gone. The global toggle (on/off/status) remains.
Regression pins cover the new no-arming contract so the old behavior cannot
silently creep back.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from argparse import Namespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import juggle_cmd_autopilot as ap  # noqa: E402
import juggle_autopilot_state as st  # noqa: E402


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = str(tmp_path / "juggle.db")
    JuggleDB(db_path=p).init_db()
    return p


@pytest.fixture
def db(db_path: str) -> JuggleDB:
    return JuggleDB(db_path=db_path)


@pytest.fixture
def flag(tmp_path: Path, monkeypatch) -> Path:
    f = tmp_path / "dotjuggle" / "autopilot"
    monkeypatch.setattr(ap, "AUTOPILOT_FLAG", f)
    return f


def _args(db_path: str, command: str, project: str | None = None, **kw) -> Namespace:
    return Namespace(
        db_path=db_path,
        autopilot_command=command,
        project=project,
        json_out=kw.get("json_out", False),
    )


# ── arm / disarm (restored 2026-06-30) ────────────────────────────────────────


def _mk_project(db, name="Alpha"):
    return db.create_project(name=name, objective=name.lower())


def test_disarm_adds_to_exclusion_set(db_path, db, flag, capsys):
    """REGRESSION PIN (2026-06-30, replaces P7 'arm exits 1'): `autopilot disarm
    <pid>` succeeds and records the project in the disarmed set."""
    pid = _mk_project(db)
    ap.cmd_autopilot(_args(db_path, "disarm", pid))
    assert st.get_disarmed_projects(db) == [pid]


def test_arm_removes_from_exclusion_set(db_path, db, flag, capsys):
    """REGRESSION PIN (2026-06-30, replaces P7 'disarm exits 1'): `autopilot arm
    <pid>` re-arms a previously disarmed project."""
    pid = _mk_project(db)
    st.disarm_project(db, pid)
    ap.cmd_autopilot(_args(db_path, "arm", pid))
    assert st.get_disarmed_projects(db) == []


def test_arm_all_clears_exclusion_set(db_path, db, flag, capsys):
    p1, p2 = _mk_project(db, "A"), _mk_project(db, "B")
    st.disarm_all(db, [p1, p2])
    ap.cmd_autopilot(_args(db_path, "arm", None))  # no project = arm-all
    assert st.get_disarmed_projects(db) == []


def test_disarm_all_excludes_every_active(db_path, db, flag, capsys):
    _mk_project(db, "A"), _mk_project(db, "B")
    ap.cmd_autopilot(_args(db_path, "disarm", None))  # no project = disarm-all
    # every active project is now disarmed
    active = {p["id"] for p in db.list_projects()}
    assert set(st.get_disarmed_projects(db)) == active


# ── on / off ─────────────────────────────────────────────────────────────────


def test_on_sets_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "on"))
    assert flag.exists()


def test_off_clears_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "on"))
    ap.cmd_autopilot(_args(db_path, "off"))
    assert not flag.exists()


def test_off_idempotent_when_already_off(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "off"))
    assert not flag.exists()


# ── status ────────────────────────────────────────────────────────────────────


def test_status_reports_global_on(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "on"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "ON" in out


def test_status_reports_global_off(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "OFF" in out


def test_status_json(db_path, db, flag, capsys):
    import json

    ap.cmd_autopilot(_args(db_path, "on"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    data = json.loads(capsys.readouterr().out)
    assert data["global_on"] is True


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_cli_registers_autopilot_subcommand():
    import subprocess

    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"), "autopilot", "--help"],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": os.environ.get("_JUGGLE_TEST_DB", "")},
    )
    assert r.returncode == 0
    for sub in ("on", "off", "status", "arm", "disarm"):
        assert sub in r.stdout


def test_cli_arm_help_succeeds_via_subprocess():
    """REGRESSION PIN (2026-06-30, replaces P7 'arm exits non-zero'): the arm
    subcommand is registered and its --help exits 0."""
    import subprocess

    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
         "autopilot", "arm", "--help"],
        capture_output=True, text=True,
        env={**os.environ},
    )
    assert r.returncode == 0


def test_status_json_reports_disarmed_set(db_path, db, flag, capsys):
    """REGRESSION PIN (2026-06-30): status --json surfaces the disarmed set and
    the derived armed set so the feature is agent-inspectable."""
    import json

    pid = _mk_project(db)
    st.disarm_project(db, pid)
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    data = json.loads(capsys.readouterr().out)
    assert data["disarmed_projects"] == [pid]
    assert pid not in data["armed_projects"]
