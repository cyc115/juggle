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


def test_coder_template_includes_incremental_commit_guidance():
    """Regression (2026-06-20): a dispatched coder agent died with ZERO output
    because it batched all work into a single never-reached final commit, losing
    everything. The coder task template MUST instruct incremental commits so an
    interrupted/crashed run preserves partial progress.

    Pinned on the in-repo DEFAULT so the rule ships with the code and cannot be
    silently lost to a missing or overriding user config.
    """
    from juggle_settings import DEFAULTS

    coder = DEFAULTS["task_templates"]["coder"]
    low = coder.lower()
    assert "commit incrementally" in low, (
        "coder template lost its incremental-commit guidance"
    )
    # Names the mechanism (committed increments survive a crash) and the safety rail.
    assert "survive" in low, "guidance must explain committed increments survive a crash"
    assert "never commit to main" in low, "guidance must keep the no-commit-to-main rail"

    # Coder-only: the rule applies only to roles that produce commits, so it
    # belongs to the role template — not the universal preamble or other roles.
    for role in ("planner", "researcher"):
        assert "commit incrementally" not in DEFAULTS["task_templates"][role].lower(), (
            f"incremental-commit guidance leaked into the {role} template"
        )

def test_coder_template_includes_verify_once_quarantine_guidance():
    """Regression (2026-06-20): 3 coder agents (a928632a, a72993f9, a198279f)
    zombie-looped — each burned 100k-330k tokens. In final verification they ran
    the FULL suite (which includes the PRE-EXISTING quarantined reds loc_gate /
    data_migration / test_integrate), saw red, concluded "not done", and/or
    launched the suite as a BACKGROUND job then polled it forever — re-spawning
    suite runs in an infinite loop, never calling complete-agent.

    The coder template MUST tell agents to: (1) deselect the quarantined tests
    when self-verifying with the full suite, (2) run the suite ONCE,
    synchronously (no background-poll, no re-run loop), and (3) end by calling
    complete-agent/fail-agent. Pinned on the in-repo DEFAULT so the rule ships
    with the code and cannot be lost to a missing/overriding user config.
    """
    from juggle_settings import DEFAULTS

    coder = DEFAULTS["task_templates"]["coder"]
    low = coder.lower()

    # (1) A dedicated Verification subsection exists.
    assert "verification" in low, "coder template lost its Verification subsection"

    # (1) Names the quarantine-deselect mechanism, and every quarantined test
    #     path the agent must deselect appears verbatim (so the guidance stays
    #     in lock-step with integrate.quarantine_tests).
    assert "--deselect" in coder, "Verification guidance must show --deselect flags"
    for qt in DEFAULTS["integrate"]["quarantine_tests"]:
        assert qt in coder, (
            f"quarantined test {qt} not named in coder Verification guidance "
            "(it must be deselected by the self-verify command)"
        )

    # (2) Run-once / synchronous / anti-loop language.
    assert "once" in low, "guidance must say run the verification suite ONCE"
    assert "background" in low and "poll" in low, (
        "guidance must forbid backgrounding the suite and polling it"
    )

    # (3) Must terminate via complete-agent / fail-agent.
    assert "complete-agent" in low and "fail-agent" in low, (
        "Verification guidance must require ending on complete-agent/fail-agent"
    )

    # Coder-only: pre-existing-reds verify guidance must NOT leak into the
    # planner/researcher templates (they don't run the suite).
    for role in ("planner", "researcher"):
        other = DEFAULTS["task_templates"][role].lower()
        assert "--deselect" not in other and "quarantine" not in other, (
            f"verify-once/quarantine guidance leaked into the {role} template"
        )
