"""Tests for `juggle monitor show-cron-spec --json` (legacy-monitor-cron plan,
2026-07-07): the CronCreate spec emitter that keeps the poll prompt + cadence
code-owned (not duplicated prose in commands/*.md).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class Args:
    json_out = True


def test_cron_spec_reflects_default_config_cadence(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    from juggle_cmd_monitor import cmd_monitor_cron_spec, POLL_PROMPT

    cmd_monitor_cron_spec(Args())
    out = json.loads(capsys.readouterr().out)

    assert out == {"cron": "*/5 * * * *", "prompt": POLL_PROMPT}


def test_cron_spec_reflects_custom_config_cadence(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"legacy_monitor": {"cadence": "*/10 * * * *"}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config))
    from juggle_cmd_monitor import cmd_monitor_cron_spec

    cmd_monitor_cron_spec(Args())
    out = json.loads(capsys.readouterr().out)

    assert out["cron"] == "*/10 * * * *"


def test_poll_prompt_carries_the_marker_and_relay_rules():
    """The marker is what enable-legacy-monitor --off matches on to find/delete
    the cron job (CronList -> CronDelete); the relay rules must stay in the
    prompt (code-owned), not duplicated prose in commands/*.md."""
    from juggle_cmd_monitor import POLL_PROMPT

    assert "[juggle legacy-monitor poll]" in POLL_PROMPT
    assert "Review ready" in POLL_PROMPT
    assert "do NOT narrate an empty poll" in POLL_PROMPT
