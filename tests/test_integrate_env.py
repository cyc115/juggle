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
    ok_absent, reason_absent, _ = run_test_cmd_full(CANARY, str(tmp_path), "cyc_probe")

    monkeypatch.setenv("JUGGLE_MAX_THREADS", "10")
    ok_present, reason_present, _ = run_test_cmd_full(CANARY, str(tmp_path), "cyc_probe")

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


# A "test_cmd" that always fails, printing one pytest-shaped FAILED line.
DET_FAIL = "sh -c 'echo \"FAILED tests/a.py::t1\"; exit 1'"


def test_failure_reason_names_the_cleared_overrides(tmp_path, monkeypatch):
    """2026-07-25 cyc_LI env-divergence incident: an env-caused divergence must
    ANNOUNCE itself. The refusal (which becomes the action item and the fail
    envelope's log_tail) must name every override integrate cleared, and tell
    the operator how to reproduce integrate's env."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    monkeypatch.setenv("JUGGLE_MAX_THREADS", "10")
    ok, reason, _ = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")

    assert ok is False
    assert "JUGGLE_MAX_THREADS=10" in reason, reason
    assert "make test-integrate" in reason, reason


def test_failure_reason_states_env_status_even_with_no_overrides(tmp_path, monkeypatch):
    """Silence is ambiguous — the env line is emitted on EVERY test failure, so
    an operator can always tell whether the environment was a factor."""
    from juggle_integrate_env import dropped_overrides
    from juggle_integrate_fullsuite import run_test_cmd_full

    for name in list(dropped_overrides()):
        monkeypatch.delenv(name, raising=False)
    ok, reason, _ = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")

    assert ok is False
    assert "env: sanitized" in reason, reason


# A "test_cmd" that fails DIFFERENTLY on its second run (marker file in cwd).
FLAKY_FAIL = (
    "sh -c 'if [ -f m ]; then echo \"FAILED tests/b.py::t2\"; "
    "else touch m; echo \"FAILED tests/a.py::t1\"; fi; exit 1'"
)


def test_failing_node_ids_parses_pytest_short_summary():
    from juggle_integrate_fullsuite import failing_node_ids

    out = (
        "=== short test summary info ===\n"
        "FAILED tests/test_x.py::test_a - AssertionError: assert 0 == 1\n"
        "ERROR tests/test_y.py::test_b\n"
        "FAILED tests/test_x.py::test_a\n"       # duplicate collapses
        "1 failed, 4323 passed in 110.94s\n"
    )
    assert failing_node_ids(out) == ["tests/test_x.py::test_a", "tests/test_y.py::test_b"]
    assert failing_node_ids("no summary here") == []


def test_retry_verdict_calls_identical_failures_deterministic(tmp_path):
    """2026-07-25 cyc_LI env-divergence incident: the retry must not imply a
    flake was ruled out. Identical failing sets across both runs => the retry
    CONFIRMED a real red."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason, _ = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")
    assert ok is False
    assert "DETERMINISTIC" in reason, reason
    assert "did NOT rule out a flake" in reason, reason
    assert "tests/a.py::t1" in reason, reason


def test_retry_verdict_calls_differing_failures_flaky(tmp_path):
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason, _ = run_test_cmd_full(FLAKY_FAIL, str(tmp_path), "cyc_probe")
    assert ok is False
    assert "FLAKY-LOOKING" in reason, reason
    assert "tests/a.py::t1" in reason and "tests/b.py::t2" in reason, reason


def test_retry_verdict_is_undetermined_when_output_is_unparseable(tmp_path):
    """Degrade gracefully — a non-pytest test_cmd must never crash the runner."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason, _ = run_test_cmd_full("sh -c 'echo boom >&2; exit 3'", str(tmp_path), "cyc_probe")
    assert ok is False
    assert "UNDETERMINED" in reason, reason


def test_run_integrate_env_script_clears_juggle_namespace(tmp_path):
    """`make test-integrate` parity (2026-07-25 cyc_LI env-divergence incident):
    the documented local command must give the suite the SAME env integrate
    does, so 'passes for me' and 'passes in integrate' cannot diverge."""
    env = dict(os.environ, JUGGLE_MAX_THREADS="10", CLAUDE_PLUGIN_DATA="/x")
    r = subprocess.run(
        [sys.executable, str(_ROOT / "scripts" / "run_integrate_env.py"),
         sys.executable, "-c",
         "import os;print(sorted(k for k in os.environ "
         "if k.startswith(('JUGGLE_','_JUGGLE_')) or k=='CLAUDE_PLUGIN_DATA'))"],
        capture_output=True, text=True, env=env, cwd=str(_ROOT),
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", f"wrapper leaked juggle vars: {r.stdout!r}"


def test_run_test_cmd_full_returns_failing_node_ids_for_signature_keying(tmp_path):
    """Adjacent to the 2026-07-25 cyc_LI incident (Task 6): every red-suite
    failure hashed to sha1('red-suite|') because integrate passed no `files=`
    to `_fail`. `run_test_cmd_full` must widen its return to carry the failing
    node ids so the per-signature repair cap can tell two different reds
    apart. RED before the fix: ValueError unpacking a 2-tuple into 3 names."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason, failing = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")
    assert ok is False
    assert failing == ["tests/a.py::t1"], failing

    ok2, reason2, failing2 = run_test_cmd_full(CANARY, str(tmp_path), "cyc_probe")
    assert ok2 is True
    assert failing2 == [], failing2


def test_red_suite_signature_differs_per_failing_test_set():
    """Adjacent to the 2026-07-25 cyc_LI incident: red-suite failures all hashed
    to sha1('red-suite|') because integrate passed no files= to _fail, so the
    per-signature repair cap could not tell two different reds apart."""
    from juggle_integrate_envelope import RED_SUITE, compute_signature

    assert compute_signature(RED_SUITE, ["tests/a.py::t1"]) != compute_signature(
        RED_SUITE, ["tests/b.py::t2"]
    )
    assert compute_signature(RED_SUITE, []) == compute_signature(RED_SUITE, [])


def test_make_test_integrate_routes_through_the_integrate_env_contract():
    """Parity PIN: if the Makefile target or the wrapper ever stops using
    sanitized_env(), local and integrate envs can silently diverge again."""
    makefile = (_ROOT / "Makefile").read_text()
    wrapper = (_ROOT / "scripts" / "run_integrate_env.py").read_text()
    runner = (_ROOT / "src" / "juggle_integrate_fullsuite.py").read_text()

    assert "test-integrate:" in makefile, "Makefile must expose a test-integrate target"
    assert "scripts/run_integrate_env.py" in makefile, (
        "test-integrate must run the suite through the integrate-env wrapper"
    )
    assert "sanitized_env" in wrapper, "the wrapper must use the shared env contract"
    assert "sanitized_env" in runner, "integrate must use the shared env contract"
