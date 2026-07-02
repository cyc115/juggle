"""Config test-isolation pins (2026-07-02 real-config-mutation incident).

INCIDENT 2026-07-02 ~06:00-06:25: tests/test_juggle_harness.py::
test_real_settings_default_harness_is_claude flapped across integrate runs
because concurrent full-suite runs (agent worktrees) TRANSIENTLY MUTATED the
real ~/.juggle/config.json (harnesses.claude.readiness_markers momentarily
missing 'bypass permissions on', then restored; config mtime churned at
06:10/06:19). Root cause: no global test isolation redirected settings/config
reads+writes off the real file (unlike the DB, which already had
`_isolate_db_from_prod`) — `juggle_cockpit._persist_ratios` in particular
writes column_ratios to `_JUGGLE_CONFIG_PATH` (defaulting to the real
~/.juggle/config.json) on every cockpit exit, so any cockpit test that didn't
individually patch the env var mutated live user config. Same isolation
defect class as the prod-DB guard (`_isolate_db_from_prod`) and the spool
guard (fix-spool-test-isolation).

These pins never perform real IO against the actual ~/.juggle directory —
even in a simulated pre-fix ("RED") run they are safe to execute.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import _is_prod_config_artifact


def test_prod_config_json_is_classified_as_prod_artifact():
    """Pure, no IO: ~/.juggle/config.json (and its atomic-write .tmp / backup
    siblings) are covered by the write guard's classifier."""
    prod_dir = Path.home() / ".juggle"
    assert _is_prod_config_artifact(prod_dir / "config.json")
    assert _is_prod_config_artifact(prod_dir / "config.json.tmp")
    assert _is_prod_config_artifact(prod_dir / "config.json.bak-pre-1.21")
    # A sibling prod artifact (agent-settings overlay, pidfile) is NOT a
    # config.json write — those are covered by their own, unrelated guards.
    assert not _is_prod_config_artifact(prod_dir / "agent-settings" / "coder.json")
    assert not _is_prod_config_artifact(prod_dir / "watchdog.pid")


def test_write_text_guard_wired_for_prod_config():
    """The autouse `_isolate_config_from_prod` fixture wraps `Path.write_text`
    globally (same hermetic per-call seam as `_guard_no_prod_artifacts`)."""
    assert getattr(Path.write_text, "_prod_artifact_guarded", False), (
        "autouse _isolate_config_from_prod did not wrap Path.write_text"
    )


def test_write_text_guard_blocks_write_under_prod_dir(tmp_path, monkeypatch):
    """End-to-end guard behavior: writing the classified prod config.json
    raises BEFORE any IO happens. Swaps in a FAKE prod dir via monkeypatch
    (never the real ~/.juggle) so this is safe to run in both RED and GREEN
    states."""
    import conftest as _conftest

    fake_prod = (tmp_path / "fake-home" / ".juggle").resolve()
    fake_prod.mkdir(parents=True)
    monkeypatch.setattr(_conftest, "_PROD_JUGGLE_DIR", fake_prod)
    target = fake_prod / "config.json"

    with pytest.raises(RuntimeError, match="TEST ISOLATION VIOLATION"):
        target.write_text("{}")
    assert not target.exists()  # guard fired BEFORE any write


def test_settings_env_redirected_away_from_real_config(tmp_path):
    """`_JUGGLE_CONFIG_PATH` is redirected to a per-test tmp file for every
    test, so any settings-writing code path defaults into tmp, never home."""
    import os

    cfg_path = Path(os.environ["_JUGGLE_CONFIG_PATH"])
    assert cfg_path.parent == tmp_path
    assert Path.home() not in (cfg_path, *cfg_path.parents)


def test_configure_db_mode_default_path_lands_in_tmp_not_home(tmp_path):
    """Representative settings-writing call (T-spool sibling to `configure_db_mode`
    used by `juggle:init`): with no explicit config_path, it must resolve via the
    redirected `_JUGGLE_CONFIG_PATH` env var — never the real ~/.juggle/config.json."""
    from juggle_cmd_db_flush import configure_db_mode

    configure_db_mode("tmpfs")

    import os

    written = Path(os.environ["_JUGGLE_CONFIG_PATH"])
    assert written.exists()
    assert written.parent == tmp_path
    assert json.loads(written.read_text())["db"]["mode"] == "tmpfs"


def test_config_dir_redirected_away_from_real_home(tmp_path):
    """2026-07-02 watchdog-flake incident: `paths.config_dir` (a SEPARATE
    settings key from `_JUGGLE_CONFIG_PATH`, consumed by
    `juggle_watchdog_inspect._config_dir` for the snapshot dir, and by
    `juggle_agent_settings._overlay_dir` for agent-settings overlays) must
    ALSO be redirected, or any test reading it (e.g. a watchdog snapshot
    write/glob-read pair) races the real live ~/.juggle/watchdog/snapshots
    dir against a concurrently running production daemon on the same
    machine — `test_stalled_silent_action_item_filed` flaked exactly this
    way in a full-suite run."""
    from juggle_settings import get_settings

    config_dir = Path(get_settings()["paths"]["config_dir"])
    assert config_dir.parent == tmp_path
    assert Path.home() not in (config_dir, *config_dir.parents)


def test_watchdog_snapshot_dir_lands_in_tmp_not_home(tmp_path):
    """Representative config_dir-reading call: the watchdog inspector's
    snapshot dir must resolve under tmp, never the real ~/.juggle/watchdog."""
    from juggle_watchdog_inspect import _config_dir

    resolved = _config_dir()
    assert resolved.parent == tmp_path
    assert Path.home() not in (resolved, *resolved.parents)
