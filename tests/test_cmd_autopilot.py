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
from juggle_graph_dispatch import ARMED_PROJECT_KEY  # noqa: E402


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


# ── P7 pins — arm/disarm must exit non-zero ───────────────────────────────────


def test_arm_exits_nonzero_p7(db_path, flag, capsys):
    """REGRESSION PIN (P7): `autopilot arm` must exit 1 — arming is removed."""
    with pytest.raises(SystemExit) as ei:
        ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    assert ei.value.code == 1


def test_disarm_exits_nonzero_p7(db_path, flag, capsys):
    """REGRESSION PIN (P7): `autopilot disarm` must exit 1 — arming is removed."""
    with pytest.raises(SystemExit) as ei:
        ap.cmd_autopilot(_args(db_path, "disarm", "INBOX"))
    assert ei.value.code == 1


def test_arm_prints_removal_message_p7(db_path, flag, capsys):
    """REGRESSION PIN (P7): arm exit message mentions P7 removal."""
    with pytest.raises(SystemExit):
        ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    err = capsys.readouterr().err
    assert "removed" in err.lower() or "P7" in err


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
    for sub in ("on", "off", "status"):
        assert sub in r.stdout


def test_cli_arm_exits_nonzero_via_subprocess():
    """REGRESSION PIN (P7): `juggle autopilot arm` via CLI exits non-zero."""
    import subprocess

    r = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
         "autopilot", "arm", "INBOX"],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": "/tmp/does-not-exist.db"},
    )
    assert r.returncode != 0
