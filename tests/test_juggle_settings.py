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


def test_get_settings_honors_config_dir_env_override(tmp_path, monkeypatch):
    """REGRESSION PIN 2026-07-01 (test-isolation): JUGGLE_CONFIG_DIR redirects
    config_dir so a test-spawned watchdog daemon writes its log + snapshot/
    recovery dirs under tmp_path, never the shared ~/.juggle/watchdog.log.

    Pre-fix get_settings ignored the env → config_dir stayed the ~/.juggle
    default → real test daemons polluted the prod watchdog log.
    """
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    monkeypatch.setenv("JUGGLE_CONFIG_DIR", str(tmp_path / "isolated"))
    from juggle_settings import get_settings

    assert get_settings()["paths"]["config_dir"] == str(tmp_path / "isolated")


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

def test_llm_profiles_defaults_are_deepseek_v4():
    from juggle_settings import DEFAULTS

    profiles = DEFAULTS["llm_profiles"]
    assert profiles["cheap"]["openrouter_model"] == "deepseek/deepseek-v4-flash"
    assert profiles["cheap"]["fallback_model"] == "claude-haiku-4-5-20251001"
    assert profiles["normal"]["openrouter_model"] == "deepseek/deepseek-v4-pro"
    assert profiles["normal"]["fallback_model"] == "sonnet"
    assert profiles["synthesis"]["openrouter_model"] == "deepseek/deepseek-v4-flash"
    assert profiles["synthesis"]["fallback_model"] == "sonnet"
    assert profiles["synthesis"]["max_tokens"] == 2048


def test_task_templates_in_defaults():
    from juggle_settings import DEFAULTS
    assert "task_templates" in DEFAULTS
    assert "coder" in DEFAULTS["task_templates"]
    assert "planner" in DEFAULTS["task_templates"]
    assert "researcher" in DEFAULTS["task_templates"]


def test_task_template_override():
    import json
    import os
    import tempfile

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

def test_coder_template_mandates_full_suite_once_no_subset():
    """Regression (2026-06-20): coder agents zombie-looped — burned 100k-330k
    tokens each — by launching the suite as a BACKGROUND job and polling it
    forever, never calling complete-agent. The earlier mitigation deselected a
    quarantine list; the 2026-06-20 user directive REPLACED that with "always
    run the FULL test suite, never a subset". The coder template MUST now tell
    agents to: (1) run the FULL suite (no subset, no --deselect), (2) run it
    ONCE, synchronously (no background-poll, no re-run loop) with a HARD-bounded
    fix-and-re-run cycle (one fix attempt, then STOP), and (3) end by calling
    complete-agent/fail-agent. The template must NOT carry any --deselect /
    quarantine routing (that would contradict the full-suite directive).
    Pinned on the in-repo DEFAULT so the rule ships with the code and cannot be
    lost to a missing/overriding user config.
    """
    from juggle_settings import DEFAULTS

    coder = DEFAULTS["task_templates"]["coder"]
    low = coder.lower()

    # (1) A dedicated Verification subsection exists and mandates the FULL suite.
    assert "verification" in low, "coder template lost its Verification subsection"
    assert "full suite" in low or "full test suite" in low, (
        "Verification guidance must mandate the FULL suite (directive 2026-06-20)"
    )

    # (1) The full-suite directive forbids subset/deselect: the template must NOT
    #     instruct agents to deselect anything.  A bare `--deselect tests/...`
    #     command (the old quarantine routing) must be absent — only descriptive
    #     "no --deselect" phrasing is allowed, so we assert no quarantine path is
    #     named as something to deselect.
    for qt in ("tests/test_loc_gate.py", "tests/test_data_migration.py"):
        assert f"--deselect {qt}" not in coder, (
            f"coder template still deselects {qt} — contradicts full-suite directive"
        )

    # (2) Run-once / synchronous / anti-loop language.
    assert "once" in low, "guidance must say run the verification suite ONCE"
    assert "background" in low and "poll" in low, (
        "guidance must forbid backgrounding the suite and polling it"
    )

    # (3) Must terminate via the completion verbs (P9 G1: 'agent complete' /
    #     'agent fail' — the legacy complete-agent/fail-agent forms were migrated).
    assert "agent complete" in low and "agent fail" in low, (
        "Verification guidance must require ending on agent complete/agent fail"
    )

    # (I2) The fix-and-re-run cycle must be HARD-bounded (one attempt, then
    #      stop) — not an open-ended "re-run once per fix" that loops forever.
    assert "second" in low and ("partial" in low or "blocker" in low), (
        "anti-loop guidance must hard-stop after one fix attempt "
        "(no second fix; bail to PARTIAL/BLOCKER) — DA I2"
    )

    # Coder-only: the verify guidance must NOT leak into the planner/researcher
    # templates (they don't run the suite).
    for role in ("planner", "researcher"):
        other = DEFAULTS["task_templates"][role].lower()
        assert "--deselect" not in other and "quarantine" not in other, (
            f"verify-once guidance leaked into the {role} template"
        )


def test_integrate_defaults_are_full_suite_no_quarantine():
    """Directive (2026-06-20): integrate ALWAYS runs the FULL suite, never a
    subset. The shipped DEFAULTS must reflect that — test_scope 'full' and an
    EMPTY quarantine list — so a fresh install (no config.json) never scopes or
    deselects. Pinned so the directive cannot silently regress in DEFAULTS."""
    from juggle_settings import DEFAULTS

    integ = DEFAULTS["integrate"]
    assert integ["test_scope"] == "full", "DEFAULTS must run the full suite"
    assert integ["quarantine_tests"] == [], (
        "DEFAULTS quarantine_tests must be empty (no subset/deselect)"
    )
