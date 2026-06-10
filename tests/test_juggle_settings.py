"""Regression tests for juggle_settings — no lru_cache, config always fresh."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_get_settings_reads_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.delenv("JUGGLE_MAX_THREADS", raising=False)
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    from juggle_settings import get_settings, DEFAULTS

    s = get_settings()
    assert s["max_threads"] == DEFAULTS["max_threads"]


def test_get_settings_loads_config_file(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"max_threads": 7}))
    monkeypatch.delenv("JUGGLE_MAX_THREADS", raising=False)
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config))
    from juggle_settings import get_settings

    assert get_settings()["max_threads"] == 7


def test_get_settings_reads_fresh_on_each_call(tmp_path, monkeypatch):
    """Regression: without lru_cache, config changes are picked up immediately."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"max_threads": 5}))
    monkeypatch.delenv("JUGGLE_MAX_THREADS", raising=False)
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config))
    from juggle_settings import get_settings

    assert get_settings()["max_threads"] == 5

    config.write_text(json.dumps({"max_threads": 99}))
    assert get_settings()["max_threads"] == 99


def test_get_settings_nested_merge(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"cockpit": {"refresh_interval_secs": 5.0}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config))
    from juggle_settings import get_settings

    s = get_settings()
    assert s["cockpit"]["refresh_interval_secs"] == 5.0
    # Other nested keys survive deep-merge
    assert "column_ratios" in s["cockpit"]


def test_get_settings_survives_corrupt_config(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("not valid json{{{")
    monkeypatch.delenv("JUGGLE_MAX_THREADS", raising=False)
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(config))
    from juggle_settings import get_settings, DEFAULTS

    s = get_settings()
    assert s["max_threads"] == DEFAULTS["max_threads"]


# ── Fix 2: task_templates ───────────────────────────────────────────────────

def test_task_templates_in_defaults():
    from juggle_settings import DEFAULTS
    assert "task_templates" in DEFAULTS
    assert "coder" in DEFAULTS["task_templates"]
    assert "planner" in DEFAULTS["task_templates"]
    assert "researcher" in DEFAULTS["task_templates"]


def test_task_template_override():
    import os, json, tempfile
    from juggle_settings import get_settings
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(json.dumps({"task_templates": {"coder": "custom"}}))
        f.flush()
        os.environ["_JUGGLE_CONFIG_PATH"] = f.name
        try:
            settings = get_settings()
            assert settings["task_templates"]["coder"] == "custom"
            assert "planner" in settings["task_templates"]
        finally:
            os.unlink(f.name)
            os.environ.pop("_JUGGLE_CONFIG_PATH", None)


def test_coder_template_includes_harness_gate():
    from juggle_settings import DEFAULTS
    coder = DEFAULTS["task_templates"]["coder"]
    assert "HARNESS GATE" in coder
