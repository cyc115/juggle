"""Integrate test-env contract pins (2026-07-25 cyc_LI env-divergence incident).

Symptom: `juggle integrate` ran the configured test_cmd with no `env=`, so the
suite inherited the CALLER's environment. A test that read the ambient
JUGGLE_MAX_THREADS passed ~17k times in operator/agent shells (which export it)
and failed 4 integrations under the watchdog daemon (which does not) — 8 commits
on cyc_LI blocked ~3 days.

The contract: integrate CLEARS juggle's own namespace (JUGGLE_*, _JUGGLE_*,
CLAUDE_PLUGIN_DATA) and passes everything else through, so the verdict cannot
depend on who invoked integrate.
"""
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))

# A "test_cmd" that FAILS iff it can see an ambient JUGGLE_MAX_THREADS. Not a
# pytest invocation, so full_suite_violations() correctly ignores it.
CANARY = (
    "python3 -c "
    "'import os,sys; sys.exit(1 if \"JUGGLE_MAX_THREADS\" in os.environ else 0)'"
)


def test_integrate_verdict_is_invariant_to_ambient_juggle_env(tmp_path, monkeypatch):
    """HEADLINE GATE (2026-07-25 cyc_LI env-divergence incident): a test_cmd that
    reads an ambient JUGGLE_* var MUST produce the SAME integrate verdict whether
    or not the caller exported it. RED before the fix: `present` returns False."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    monkeypatch.delenv("JUGGLE_MAX_THREADS", raising=False)
    ok_absent, reason_absent = run_test_cmd_full(CANARY, str(tmp_path), "cyc_probe")

    monkeypatch.setenv("JUGGLE_MAX_THREADS", "10")
    ok_present, reason_present = run_test_cmd_full(CANARY, str(tmp_path), "cyc_probe")

    assert ok_absent == ok_present, (
        "integrate's verdict changed with the CALLER's ambient JUGGLE_MAX_THREADS "
        f"(absent -> {ok_absent}: {reason_absent!r}; "
        f"present -> {ok_present}: {reason_present!r})"
    )
    assert ok_absent is True, f"canary should pass under a sanitized env: {reason_absent!r}"


def test_controlled_namespace_is_juggle_only():
    """The deny-list covers juggle's namespace and CLAUDE_PLUGIN_DATA — and
    NOTHING the toolchain needs (an allow-list here would be whack-a-mole)."""
    from juggle_integrate_env import is_controlled

    for name in (
        "JUGGLE_MAX_THREADS", "JUGGLE_MAX_BACKGROUND_AGENTS", "JUGGLE_DB_PATH",
        "JUGGLE_IS_AGENT", "JUGGLE_ORCHESTRATOR", "JUGGLE_SPOOL_DIR",
        "_JUGGLE_CONFIG_PATH", "_JUGGLE_TEST_DB", "CLAUDE_PLUGIN_DATA",
    ):
        assert is_controlled(name), f"{name} must be cleared by integrate"

    for name in (
        "HOME", "PATH", "TMPDIR", "USER", "SHELL", "LANG", "TERM",
        "UV_CACHE_DIR", "XDG_CACHE_HOME", "GIT_DIR", "SSH_AUTH_SOCK", "CI",
        "OPENROUTER_KEY", "VOYAGE_API_KEY", "DEEPSEEK_API_KEY",
    ):
        assert not is_controlled(name), f"{name} must pass through to the suite"


def test_sanitized_env_clears_controlled_and_passes_the_rest_through():
    from juggle_integrate_env import dropped_overrides, sanitized_env

    src = {
        "JUGGLE_MAX_THREADS": "10",
        "_JUGGLE_CONFIG_PATH": "/x/config.json",
        "CLAUDE_PLUGIN_DATA": "/x/juggle",
        "HOME": "/home/me",
        "PATH": "/usr/bin",
        "OPENROUTER_KEY": "sk-secret",
    }
    assert sanitized_env(src) == {
        "HOME": "/home/me", "PATH": "/usr/bin", "OPENROUTER_KEY": "sk-secret",
    }
    assert dropped_overrides(src) == {
        "JUGGLE_MAX_THREADS": "10",
        "_JUGGLE_CONFIG_PATH": "/x/config.json",
        "CLAUDE_PLUGIN_DATA": "/x/juggle",
    }


def test_sanitized_env_never_pins_a_value():
    """Clearing, never pinning (2026-07-25 incident): a pinned value would keep
    the ambient coupling alive and could turn a false RED into a false GREEN."""
    from juggle_integrate_env import is_controlled, sanitized_env

    out = sanitized_env({"HOME": "/home/me"})
    assert not [k for k in out if is_controlled(k)], (
        f"sanitized_env must not INJECT any controlled variable; got {out!r}"
    )


def test_env_report_never_leaks_a_non_controlled_variable():
    """~/.juggle/.env credentials are loaded into os.environ by juggle_cli; the
    failure report must name ONLY juggle-namespace variables, so no secret can
    reach an action item or the DB (2026-07-25 incident, DA finding)."""
    from juggle_integrate_env import dropped_overrides, format_env_report

    src = {"JUGGLE_MAX_THREADS": "10", "OPENROUTER_KEY": "sk-do-not-leak"}
    report = format_env_report(dropped_overrides(src))
    assert "JUGGLE_MAX_THREADS=10" in report
    assert "sk-do-not-leak" not in report
    assert "OPENROUTER_KEY" not in report
