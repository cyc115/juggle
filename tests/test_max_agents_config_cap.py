"""Regression tests for #5045 — config `max_agents` authoritative for the pool cap.

The 2026-07-01 integrate storm reached 12 coders because the watchdog daemon's
cap came from a stale inherited env `JUGGLE_MAX_BACKGROUND_AGENTS` and config
`max_agents` (lowered to 2) was ignored. These tests pin the single resolution
path (`resolve_max_agents`) and the cockpit spawn seam that pins the daemon's env
from CONFIG, so a stale inherited env can never inflate the cap above config.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import juggle_settings as S  # noqa: E402


def test_resolve_max_agents_from_config():
    """Config max_agents is the source of truth when no env override is set."""
    assert S.resolve_max_agents(env={}, config={"max_agents": 3}) == 3


def test_resolve_max_agents_env_override_respected():
    """An explicit env override still wins (optional override)."""
    assert (
        S.resolve_max_agents(
            env={"JUGGLE_MAX_BACKGROUND_AGENTS": "7"},
            config={"max_agents": 3},
        )
        == 7
    )


def test_resolve_max_agents_default():
    """No env, no config → hardcoded default."""
    assert S.resolve_max_agents(env={}, config={}) == S.DEFAULTS["max_agents"]


def test_resolve_max_agents_invalid_falls_back():
    """Garbage never crashes — invalid env falls to config, invalid config to default."""
    assert (
        S.resolve_max_agents(
            env={"JUGGLE_MAX_BACKGROUND_AGENTS": "nope"},
            config={"max_agents": 3},
        )
        == 3
    )
    assert S.resolve_max_agents(env={}, config={"max_agents": "x"}) == S.DEFAULTS["max_agents"]


def test_resolve_max_agents_reads_disk_config(tmp_path, monkeypatch):
    """With env/config omitted, reads os.environ + config.json (_JUGGLE_CONFIG_PATH)."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"max_agents": 4}')
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))
    monkeypatch.delenv("JUGGLE_MAX_BACKGROUND_AGENTS", raising=False)
    assert S.resolve_max_agents() == 4
    # explicit env override still wins over disk config
    monkeypatch.setenv("JUGGLE_MAX_BACKGROUND_AGENTS", "9")
    assert S.resolve_max_agents() == 9


def test_start_watchdog_detached_pins_cap_from_config(tmp_path, monkeypatch):
    """The daemon spawn pins JUGGLE_MAX_BACKGROUND_AGENTS from CONFIG, ignoring a
    stale inherited env — this is what makes config authoritative for the daemon."""
    import juggle_watchdog_singleton as ws

    cfg = tmp_path / "config.json"
    cfg.write_text('{"max_agents": 4}')
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))
    # A stale/inflated inherited env must NOT leak into the daemon.
    monkeypatch.setenv("JUGGLE_MAX_BACKGROUND_AGENTS", "99")

    captured = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ws.start_watchdog_detached(str(tmp_path / "juggle.db"), repo_path=str(tmp_path))

    assert captured["kwargs"]["env"]["JUGGLE_MAX_BACKGROUND_AGENTS"] == "4"
