"""Tests for `juggle autopilot` — arming authority + flag cache (Phase 4, DA M6).

The settings-table key `autopilot_armed_project` is the SOLE arming
authority; ~/.juggle/autopilot stays an existence-only cache for the global
toggle. Includes the M6 pin: arming while the global flag is already ON must
ARM, never invert the flag off (the old rm-as-disarm flip logic).
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
from dbops import db_graph as g  # noqa: E402


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


# ── arm ───────────────────────────────────────────────────────────────────────


def test_arm_sets_setting_and_creates_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    assert db.get_setting(ARMED_PROJECT_KEY) == "INBOX"
    assert flag.exists(), "global flag cache must be created on arm"
    out = capsys.readouterr().out
    assert "INBOX" in out and "armed" in out.lower()


def test_arm_while_flag_on_keeps_flag_no_rm_inversion(db_path, db, flag, capsys):
    """2026-06-10 DA M6 pin: `/juggle:toggle-autopilot P` while autopilot is
    already ON must ARM the project — the legacy flip logic would have
    rm'd the flag and silently turned autopilot OFF instead."""
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    assert flag.exists(), "arming while ON must not remove the global flag"
    assert db.get_setting(ARMED_PROJECT_KEY) == "INBOX"


def test_arm_unknown_project_fails_loud(db_path, db, flag, capsys):
    with pytest.raises(SystemExit) as ei:
        ap.cmd_autopilot(_args(db_path, "arm", "NOPE"))
    assert ei.value.code == 1
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert not flag.exists()


def test_arm_prints_spec_path_and_node_counts(db_path, db, flag, capsys):
    g.create_node(db, node_id="a", project_id="INBOX", title="A", prompt="do a")
    g.recompute_ready(db, "INBOX")
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    out = capsys.readouterr().out
    assert "INBOX-graph.md" in out  # decomposition spec path convention (DA m3)
    assert "1" in out  # node count surfaces


def test_arm_warns_when_no_graph_loaded(db_path, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    out = capsys.readouterr().out
    assert "no graph" in out.lower()


# ── disarm / off / on ─────────────────────────────────────────────────────────


def test_disarm_clears_setting_keeps_global_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "disarm"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert flag.exists(), "disarm is project-level; global flag stays"


def test_off_clears_setting_and_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    ap.cmd_autopilot(_args(db_path, "off"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert not flag.exists()


def test_off_idempotent_when_already_off(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "off"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None
    assert not flag.exists()


def test_on_sets_flag_without_arming(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "on"))
    assert flag.exists()
    assert db.get_setting(ARMED_PROJECT_KEY) is None


# ── status ────────────────────────────────────────────────────────────────────


def test_status_reports_armed_project_and_flag(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "ON" in out and "INBOX" in out


def test_status_json(db_path, db, flag, capsys):
    import json

    ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    capsys.readouterr()
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    data = json.loads(capsys.readouterr().out)
    assert data["global_on"] is True
    assert data["armed_project"] == "INBOX"


def test_status_disarmed(db_path, db, flag, capsys):
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "OFF" in out


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
    for sub in ("arm", "disarm", "on", "off", "status"):
        assert sub in r.stdout


# ── DA round-2 (2026-06-10): PR-mode refusal + divergence warning ──────────────


def test_arm_refuses_pr_push_mode_repo(db_path, db, flag, monkeypatch, capsys):
    """REGRESSION PIN (DA round-2 MAJOR-2, 2026-06-10): arming autopilot in a
    push_mode='pr' repo lets integrate mark nodes 'verified' without merging —
    dependents hydrate against a main that does not contain their deps.
    `autopilot arm` must refuse with a clear error."""
    import juggle_cmd_graph as cg
    import juggle_settings

    monkeypatch.setattr(cg, "_git_root", lambda cwd: "/fake/pr-repo")
    monkeypatch.setattr(
        juggle_settings,
        "get_repo_config",
        lambda p: {"push_mode": "pr", "test_cmd": ""},
    )
    with pytest.raises(SystemExit):
        ap.cmd_autopilot(_args(db_path, "arm", "INBOX"))
    assert db.get_setting(ARMED_PROJECT_KEY) is None  # NOT armed
    assert not flag.exists()
    err = capsys.readouterr().err
    assert "push_mode='pr'" in err and "not supported" in err


def test_status_warns_when_setting_and_flag_diverge(db_path, db, flag, capsys):
    """REGRESSION PIN (DA round-2 minor 5, 2026-06-10): a project armed in the
    settings table while the global flag file is absent means hooks inject
    nothing while the tick still dispatches — silent split-brain. `autopilot
    status` must call out the divergence."""
    db.set_setting(ARMED_PROJECT_KEY, "INBOX")  # armed, but flag file missing
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "WARNING" in out and "diverge" in out.lower()


def test_status_json_reports_divergence(db_path, db, flag, capsys):
    import json

    db.set_setting(ARMED_PROJECT_KEY, "INBOX")
    ap.cmd_autopilot(_args(db_path, "status", json_out=True))
    data = json.loads(capsys.readouterr().out)
    assert data["diverged"] is True


def test_status_no_warning_when_consistent(db_path, db, flag, capsys):
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    db.set_setting(ARMED_PROJECT_KEY, "INBOX")
    ap.cmd_autopilot(_args(db_path, "status"))
    out = capsys.readouterr().out
    assert "WARNING" not in out
