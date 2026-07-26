# Integrate Pinned Test Env — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `juggle integrate`'s test run environment-deterministic, so the same commit on the same branch always produces the same merge/no-merge verdict regardless of who invoked integrate — and, when it does not, say so loudly in the failure itself.

**Architecture:** A new single-purpose module `src/juggle_integrate_env.py` owns ONE thing: the env contract. It **clears juggle's own namespace** (`JUGGLE_*`, `_JUGGLE_*`, `CLAUDE_PLUGIN_DATA`) and **passes everything else through** — a deny-list, never an allow-list, never a pin. `juggle_integrate_fullsuite.py` keeps the run/retry/refusal concern and gains two diagnosability behaviours: it always reports which overrides it cleared, and it turns the existing blind retry into a *deterministic-vs-flaky discriminator* instead of a doubled wait that implies a flake was ruled out. `scripts/run_integrate_env.py` + `make test-integrate` reproduce integrate's exact env locally through the **same** `sanitized_env()` code path, so "passes for me" and "passes in integrate" cannot silently diverge again.

**Tech Stack:** Python 3.12+ (stdlib only in the new modules), pytest + pytest-xdist, GNU/BSD-portable `make`, POSIX `sh`.

---

## Background — the proven incident (2026-07-25, branch `cyc_LI`)

`src/juggle_integrate_fullsuite.py:59` ran the configured `test_cmd` with **no `env=`**:

```python
result = subprocess.run(test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path)
```

so the suite inherited whatever environment the **caller** happened to have. Callers differ:

| Caller | How integrate is reached | Env characteristics |
|---|---|---|
| Watchdog daemon | `juggle_watchdog_daemon` → graph tick → `_run_integrate` | Daemon env; sets `JUGGLE_ORCHESTRATOR=1` (`juggle_watchdog_daemon.py:351`); **no** `JUGGLE_MAX_THREADS` |
| Operator shell | `juggle integrate <thread>` | Whatever the shell exported — **the repo's own boilerplate exports `JUGGLE_MAX_THREADS=10`** |
| Agent pane | tmux pane started with `env -u CLAUDE_PLUGIN_DATA JUGGLE_IS_AGENT=1 JUGGLE_AGENT_ROLE=… ` (`juggle_harness_defaults.py:25`, `juggle_tmux.py:129`) | Agent-context markers set |
| Any CLI path | `src/juggle_cli.py:29-35` loads `~/.juggle/.env` into `os.environ` via `setdefault` at import | Adds `OPENROUTER_KEY`, `VOYAGE_API_KEY`, `DEEPSEEK_API_KEY`, … **that the daemon path does not load** |

The failing test read the AMBIENT `JUGGLE_MAX_THREADS` (`juggle_settings.py:334-335` treats it as a live override of `max_threads`) to make `create_thread` hit the cap. Every human/agent shell exports it, so the test passed ~17k times by hand; the watchdog's env lacks it, the cap resolved higher, creation succeeded, and the assertion inverted. **4 failed integrations, 8 commits blocked ~3 days, and the cause was misreported twice.** A separate coder (thread `LI`) is fixing that one test and sweeping the suite for siblings — **that work is NOT in this plan.** This plan makes the whole class impossible.

### Measured facts this plan relies on (verified 2026-07-25 on `cyc_LR` @ `78ac295`)

1. **The FULL suite is already green under a sanitized env.**
   `env -u JUGGLE_IS_AGENT -u JUGGLE_AGENT_ROLE -u JUGGLE_REPO_ROOT -u CLAUDE_PLUGIN_DATA uv run pytest -n auto --dist loadgroup -m "not watchdog_proc"` → **`4324 passed, 7 skipped in 110.94s`**. The fast tier likewise: `3596 passed, 7 skipped in 73.01s`. So clearing juggle's namespace does not break the suite on this branch.
2. **`CLAUDE_PLUGIN_DATA` is not read by product code.** `grep -rn CLAUDE_PLUGIN_DATA src/` returns only *docstrings and the agent-harness `env_unset` list* — i.e. juggle already deliberately **unsets** it for agent panes. Tests that need it `monkeypatch.setenv` it themselves.
3. **`CLAUDE.md`'s "Required environment variables (no defaults)" block is stale.** It names `CLAUDE_PLUGIN_DATA (juggle_cli.py)` and `JUGGLE_MAX_BACKGROUND_AGENTS, JUGGLE_MAX_THREADS (juggle_db.py)`. `src/juggle_cli.py` never reads `CLAUDE_PLUGIN_DATA`; `src/juggle_db.py` reads neither `JUGGLE_MAX_*` (its only env reference is a `JUGGLE_DB_PATH` comment at line 107). Fact 1 proves none is required. **That stale boilerplate is what taught every operator to export the poison variable.** (See Open Question 1 — this plan does not edit `CLAUDE.md`.)
4. **`~/.juggle/.env` holds live credentials** (`OPENROUTER_KEY`, `VOYAGE_API_KEY`, `DEEPSEEK_API_KEY`) and `juggle_cli` loads them into `os.environ`. Any "record the effective env in the failure envelope" design would write those into an action item and the DB. **This plan therefore reports only CONTROLLED (juggle-namespace) variables — never the full env.**
5. **pytest's `-q` output ends with a `short test summary info` block containing `FAILED <nodeid>` lines by default** (verified: `FAILED …/test_x.py::test_a` / `1 failed in 0.05s`). Node-id parsing in Task 4 is therefore sound, and degrades to `UNDETERMINED` when it isn't.

---

## Design decision — why "clear juggle's namespace, pass everything else"

Four options were weighed (the brief's 1–4).

| Option | Verdict | Why |
|---|---|---|
| **1. Pin a constructed env from juggle's resolved settings** (e.g. inject `JUGGLE_MAX_THREADS=10`) | **REJECTED as the mechanism** | It makes the verdict *stable* but keeps the ambient coupling *alive*. Worse, it converts today's false RED into a **false GREEN**: the `cyc_LI` test would go green in integrate while still being broken for anyone running bare `pytest` on a fresh checkout. A false green merges broken code — strictly worse than a false red, which merely blocks it. |
| **2. Clear the `JUGGLE_*` / `CLAUDE_PLUGIN_DATA` overrides** | **CHOSEN (primary)** | Clearing cannot *invent* a passing condition — it only removes overrides, so the suite runs against `DEFAULTS` + `config.json`, exactly what a fresh checkout does. A test that then goes red is genuinely non-hermetic, which is the correct outcome. Empirically green today (Fact 1). |
| **3. Refuse when ambient `JUGGLE_*` overrides are present** (the `full_suite_violations` pattern) | **REJECTED as the mechanism, ADOPTED as the report** | Backwards in practice: the watchdog — the primary caller — has a clean env and would *never* trip it, while every operator and agent pane (which do export `JUGGLE_MAX_THREADS`, per the repo's own boilerplate) would be refused. It would block integrate universally without making a single verdict more deterministic. But its *virtue* — loudness — is the real lesson, so Task 3 keeps it: the exact set of cleared overrides is stated in every test-failure refusal. Loud where loudness helps; silent where refusing would only obstruct. |
| **4. Combination** | **CHOSEN** | 2 (determinism) + 3's loudness (diagnosability) + Task 4 (retry honesty) + Task 5 (`make test` parity). |

### The env contract

**CONTROLLED — always cleared:** `JUGGLE_*`, `_JUGGLE_*`, `CLAUDE_PLUGIN_DATA`.
These are juggle's own knobs. A test that reads one ambiently is non-hermetic *by definition*: it must pin what it needs, exactly as `tests/conftest.py` already pins `JUGGLE_DB_PATH`, `_JUGGLE_CONFIG_PATH`, `JUGGLE_CONFIG_DIR`, `JUGGLE_SPOOL_DIR`, `JUGGLE_ORCHESTRATOR`, and `JUGGLE_WATCHDOG_DISABLE_SPAWN`. `CLAUDE_PLUGIN_DATA` joins them because product code never reads it (Fact 2) and juggle already unsets it for agent panes — clearing it in integrate makes the two paths consistent.

**PASS-THROUGH — inherited verbatim:** everything else. `HOME` (uv cache, git config, `Path.home()`), `PATH`, `TMPDIR`, `USER`, `SHELL`, `LANG`/`LC_*`, `TERM`, `UV_*`, `XDG_*`, `GIT_*`, `SSH_AUTH_SOCK`, `CI`, …

**Why a deny-list and not an allow-list:** the set juggle owns is small, knowable, and stable (one namespace prefix plus one name). The set `uv`, `git`, `pytest-xdist`, `cairosvg`, and the OS toolchain need is neither — an allow-list would be endless whack-a-mole whose failures (a missing `UV_CACHE_DIR`, a missing `SSH_AUTH_SOCK`) are far more damaging than the bug it fixes. A deny-list also means this module never has to reason about secrets: the `~/.juggle/.env` credentials keep flowing through untouched and are never named in any report.

**Why clear and never pin:** see option 1 above. Determinism comes from *removing* the input, not from *choosing* a value for it.

---

## Global Constraints

Every task's requirements implicitly include this section.

- **Workspace:** `/tmp/juggle-juggle-LR`, branch `cyc_LR`. `cd` there before any git or file operation.
- **Full suite GREEN at every commit.** `make test` — expected baseline on this branch: `4324 passed, 7 skipped` (the count grows as this plan's tests land; nothing may fail or error).
- **LOC gate ≤300 lines** per `src/**/*.py` and per Python script in `scripts/` (`scripts/loc_gate.py`, `LIMIT = 300`). The allowlist may only shrink — never add an entry. `src/juggle_integrate_fullsuite.py` is 72 lines today and must end this plan **under 300**; `src/juggle_integrate_env.py` must stay well under it.
- **Regression-pin gate:** every fix here adds a pinned test whose docstring names the incident — `2026-07-25 cyc_LI integrate env-divergence incident` — demonstrates RED before the fix, and lives in the standard suite (no skip/opt-in marker).
- **POSIX-portable:** macOS and Debian. No GNU-only flags, no `bash`-isms in `sh -c` fixtures, no `env -0`.
- **Stdlib only** in `src/juggle_integrate_env.py` and `scripts/run_integrate_env.py`.
- **Do NOT modify** `AGENTS.md`, `CLAUDE.md`, or any `.codegraph` file.
- **Landing policy:** this change adds no DB migration and touches no external/security surface → land on `main` by ff-merge. Do NOT open a PR.
- **Commits:** `refactor(...)` for behaviour-preserving, `fix(...)` for behaviour change. Commit after each task.
- **Before `complete-agent`:** invoke the `mike:pre-pr` skill to run the quality gate. Do NOT open a PR.
- After code changes: `graphify update .`

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `src/juggle_integrate_env.py` | **Create** (~75 lines) | The env contract, and nothing else: which variables integrate controls, the sanitized mapping, and the human-readable report of what was cleared. Pure functions, stdlib only, no juggle imports — unit-testable without a DB. |
| `src/juggle_integrate_fullsuite.py` | **Modify** (72 → ~155 lines) | Keeps the run/retry/refusal concern. Gains: an extracted `_run_once`, the sanitized `env=`, the cleared-overrides report in the refusal, and the retry's deterministic-vs-flaky verdict. |
| `scripts/run_integrate_env.py` | **Create** (~40 lines) | `exec`-wrapper: run any command under `sanitized_env()`. The single mechanism behind `make test-integrate`, so local parity uses integrate's code, not a copy of its rules. |
| `Makefile` | **Modify** | New `test-integrate` target. |
| `docs/ARCHITECTURE.md` | **Modify** | Document the env contract next to the LOC-gate policy. |
| `tests/test_integrate_env.py` | **Create** | All pins for this plan: the headline verdict-invariance gate, the contract unit tests, the diagnostic-report gate, the retry-verdict gate, and the `make test`-parity pins. |
| `tests/test_integrate.py` | **Modify** (Task 6 only) | One test-double signature update. |

---

## Task 1: Refactor — extract the run seam (behaviour-preserving)

**Why first:** `run_test_cmd_full` currently inlines `subprocess.run(...)` twice (the call and its retry). Every subsequent task needs to change *how* that subprocess is launched and *what is compared between the two runs*. Extracting the single launch point first means Tasks 2–4 each touch one small function instead of duplicating a change across two call sites — and it lands the explicit `env=` keyword as a **verified no-op**, so the behaviour flip in Task 2 is a one-line, reviewable diff.

`src/juggle_integrate_fullsuite.py` is 72 lines — comfortably inside the 300-line gate — and already owns exactly this seam, so no split is warranted yet. Re-check after Task 4 (projected ~155 lines; still inside budget).

**Files:**
- Modify: `src/juggle_integrate_fullsuite.py:40-72`

**Interfaces:**
- Produces: `_run_once(test_cmd: str, worktree_path: str, env: dict[str, str] | None) -> subprocess.CompletedProcess[str]` — the single subprocess launch point. `run_test_cmd_full(test_cmd: str, worktree_path: str, worktree_branch: str) -> tuple[bool, str]` is unchanged.

- [ ] **Step 1: Add `_run_once` above `run_test_cmd_full`**

```python
def _run_once(
    test_cmd: str, worktree_path: str, env: dict[str, str] | None
) -> subprocess.CompletedProcess:
    """The ONE place integrate launches ``test_cmd`` (call + retry share it).

    ``test_cmd`` is run VERBATIM under ``shell=True`` — the 2026-06-20
    no-munging directive. Only the ENVIRONMENT is integrate's to control.
    """
    return subprocess.run(
        test_cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=worktree_path,
        env=env,
    )
```

- [ ] **Step 2: Route both existing calls through it (no behaviour change)**

Replace the two inline `subprocess.run(...)` blocks in `run_test_cmd_full` with:

```python
    env = dict(os.environ)  # explicit inheritance today; Task 2 sanitizes it
    result = _run_once(test_cmd, worktree_path, env)
    if result.returncode != 0:
        # One retry for transient flakes (pilot/Textual tests flake under load).
        result = _run_once(test_cmd, worktree_path, env)
```

Add `import os` beside `import subprocess` at the top of the module.

> `env=dict(os.environ)` is byte-identical in effect to the previous `env=None` on POSIX: both hand the child the same mapping. This step changes no verdict — it only makes the environment an explicit, replaceable argument.

- [ ] **Step 3: Verify the existing pins still pass**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_marker_tier.py tests/test_integrate.py -q`
Expected: all pass. In particular `test_integrate_invokes_fullsuite_guard_and_runs_verbatim` must stay green — it greps for the literal `"test_cmd, shell=True"` in this module, which `_run_once` preserves on one line.

- [ ] **Step 4: Verify the LOC gate**

Run: `cd /tmp/juggle-juggle-LR && python3 scripts/loc_gate.py; echo "exit=$?"`
Expected: `exit=0`.

- [ ] **Step 5: Full suite**

Run: `cd /tmp/juggle-juggle-LR && make test`
Expected: `4324 passed, 7 skipped` (0 failed, 0 errors).

- [ ] **Step 6: Commit**

```bash
cd /tmp/juggle-juggle-LR
git add src/juggle_integrate_fullsuite.py
git commit -m "refactor(integrate): extract _run_once as the single test_cmd launch point

Behaviour-preserving. env=dict(os.environ) is identical in effect to the
previous env=None on POSIX; it makes the suite environment an explicit,
replaceable argument so the env contract can land in one reviewable diff."
```

---

## Task 2: The fix — sanitize integrate's test environment

**Files:**
- Create: `src/juggle_integrate_env.py`
- Modify: `src/juggle_integrate_fullsuite.py` (the `env = ` line from Task 1)
- Test: `tests/test_integrate_env.py`

**Interfaces:**
- Produces:
  - `CONTROLLED_PREFIXES: tuple[str, ...]`, `CONTROLLED_NAMES: frozenset[str]`
  - `is_controlled(name: str) -> bool`
  - `dropped_overrides(source: Mapping[str, str] | None = None) -> dict[str, str]`
  - `sanitized_env(source: Mapping[str, str] | None = None) -> dict[str, str]`
  - `format_env_report(dropped: Mapping[str, str]) -> str` (used by Task 3)
- Consumes: nothing (stdlib only).

- [ ] **Step 1: Write the failing headline test**

Create `tests/test_integrate_env.py`:

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: **FAIL** — `AssertionError: integrate's verdict changed with the CALLER's ambient JUGGLE_MAX_THREADS (absent -> True: ''; present -> False: "Tests failed (exit 1) …")`.

- [ ] **Step 3: Create `src/juggle_integrate_env.py`**

```python
"""Juggle — integrate test-env contract (2026-07-25 cyc_LI env-divergence incident).

`integrate` decides merge/no-merge by running the repo's configured `test_cmd`.
Before this module it ran with `env=None`: the suite inherited whatever
environment the CALLER happened to have — the watchdog daemon, an operator
shell, an agent pane, plus `~/.juggle/.env` when the caller reached integrate
through `juggle_cli` (which loads that file into os.environ at import). Those
differ, so the same commit could be green by hand and red in integrate. It cost
4 failed integrations of cyc_LI (8 commits blocked ~3 days): a test read the
ambient JUGGLE_MAX_THREADS=10 that every operator/agent shell exports and the
watchdog daemon does not.

THE CONTRACT — integrate CLEARS juggle's own namespace and passes EVERYTHING
else through.

  CONTROLLED (always cleared) — JUGGLE_*, _JUGGLE_*, CLAUDE_PLUGIN_DATA.
      These are juggle's own knobs. A test that reads one ambiently is
      non-hermetic by definition: it must pin what it needs, exactly as
      tests/conftest.py already pins JUGGLE_DB_PATH, _JUGGLE_CONFIG_PATH,
      JUGGLE_CONFIG_DIR, JUGGLE_SPOOL_DIR, JUGGLE_ORCHESTRATOR and
      JUGGLE_WATCHDOG_DISABLE_SPAWN. CLAUDE_PLUGIN_DATA joins them because no
      product code reads it (only the agent harness, which already UNSETS it —
      juggle_harness_defaults.env_unset), so clearing it here makes the agent
      and integrate paths consistent.

      CLEARED, never PINNED. Pinning a value (e.g. injecting
      JUGGLE_MAX_THREADS=10) would keep the ambient coupling alive and could
      turn today's false RED into a false GREEN — merging code that is broken
      on a fresh checkout. Removing the input is what makes the verdict
      deterministic; choosing a value for it is not.

  PASS-THROUGH (inherited verbatim) — everything else: HOME, PATH, TMPDIR,
      USER, SHELL, LANG/LC_*, TERM, UV_*, XDG_*, GIT_*, SSH_AUTH_SOCK, CI, ...
      A deny-list, NOT an allow-list: the set juggle owns is small and knowable;
      the set uv/git/pytest-xdist/the toolchain need is not, and an allow-list
      would be endless whack-a-mole whose failures are worse than the bug it
      fixes.

A deny-list also means this module never reasons about secrets: ~/.juggle/.env
credentials (OPENROUTER_KEY, VOYAGE_API_KEY, DEEPSEEK_API_KEY) flow through
untouched, and `format_env_report` only ever names CONTROLLED variables — so no
credential can reach an action item, an event, or the DB.

Reproduce integrate's exact env locally: `make test-integrate`
(scripts/run_integrate_env.py routes through this same `sanitized_env()`).

Owns ONLY the env contract — not the suite run, the retry, or the refusal
(juggle_integrate_fullsuite), and not the integrate pipeline
(juggle_cmd_integrate).
"""
from __future__ import annotations

import os
from collections.abc import Mapping

#: Variables integrate CONTROLS: juggle's own namespace, cleared before the suite runs.
CONTROLLED_PREFIXES: tuple[str, ...] = ("JUGGLE_", "_JUGGLE_")
CONTROLLED_NAMES: frozenset[str] = frozenset({"CLAUDE_PLUGIN_DATA"})

#: Report clip for a cleared variable's value (paths can be long; no secrets here).
_VALUE_CLIP = 60


def is_controlled(name: str) -> bool:
    """True if integrate CLEARS ``name`` before running the suite."""
    return name in CONTROLLED_NAMES or name.startswith(CONTROLLED_PREFIXES)


def dropped_overrides(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The CONTROLLED variables present in ``source`` (default ``os.environ``).

    This is exactly what integrate is about to clear — the diagnostic payload
    for a failing suite, and the only env content ever surfaced to a human.
    """
    src = os.environ if source is None else source
    return {k: v for k, v in src.items() if is_controlled(k)}


def sanitized_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """``source`` (default ``os.environ``) with every CONTROLLED variable removed.

    Everything else is passed through verbatim — see the module docstring for
    why this is a deny-list and why nothing is ever pinned.
    """
    src = os.environ if source is None else source
    return {k: v for k, v in src.items() if not is_controlled(k)}


def _clip(value: str) -> str:
    return value[:_VALUE_CLIP] + "…" if len(value) > _VALUE_CLIP else value


def format_env_report(dropped: Mapping[str, str]) -> str:
    """One human line naming what integrate cleared — ALWAYS emitted on failure.

    Emitted even when nothing was dropped: silence is ambiguous, and the whole
    point of the 2026-07-25 incident is that an env-caused divergence must
    announce itself instead of looking like an ordinary red.
    """
    if not dropped:
        return (
            "env: sanitized — the caller had NO juggle overrides set, so the "
            "suite ran on config.json + DEFAULTS."
        )
    items = ", ".join(f"{k}={_clip(v)}" for k, v in sorted(dropped.items()))
    return (
        "env: sanitized — integrate CLEARED these caller overrides before "
        f"running the suite: {items}. If this suite passes in YOUR shell but "
        "fails here, a test is reading one of them ambiently instead of pinning "
        "it. Reproduce integrate's exact env with `make test-integrate`."
    )
```

- [ ] **Step 4: Wire it into the runner**

In `src/juggle_integrate_fullsuite.py`, replace the Task-1 line

```python
    env = dict(os.environ)  # explicit inheritance today; Task 2 sanitizes it
```

with

```python
    env = sanitized_env()
```

and add the import beneath `import subprocess`:

```python
from juggle_integrate_env import sanitized_env
```

`import os` from Task 1 is now unused in this module — remove it.

Update the module docstring's closing line to read:

```
pipeline (juggle_cmd_integrate) and not the env contract (juggle_integrate_env).
```

- [ ] **Step 5: Run the headline test to verify it passes**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: `1 passed`.

- [ ] **Step 6: Add the contract unit pins**

Append to `tests/test_integrate_env.py`:

```python
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
```

- [ ] **Step 7: Run the new pins**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: `5 passed`.

- [ ] **Step 8: LOC gate + full suite**

Run: `cd /tmp/juggle-juggle-LR && python3 scripts/loc_gate.py && make test`
Expected: loc gate `exit=0`; suite `4329 passed, 7 skipped` (baseline 4324 + 5 new).

- [ ] **Step 9: Commit**

```bash
cd /tmp/juggle-juggle-LR
git add src/juggle_integrate_env.py src/juggle_integrate_fullsuite.py tests/test_integrate_env.py
git commit -m "fix(integrate): run test_cmd under a sanitized, deterministic env

integrate ran the suite with env=None, inheriting the caller's environment —
watchdog daemon, operator shell and agent pane all differ. A test reading the
ambient JUGGLE_MAX_THREADS passed ~17k times by hand and failed 4 integrations
of cyc_LI (8 commits blocked ~3 days).

The contract (juggle_integrate_env): CLEAR juggle's own namespace (JUGGLE_*,
_JUGGLE_*, CLAUDE_PLUGIN_DATA), pass everything else through. Cleared, never
pinned — pinning would keep the coupling alive and could turn a false red into
a false green."
```

---

## Task 3: Diagnosability — say what was cleared, in the refusal itself

**Why:** today an env-caused failure is invisible. The operator sees `1 failed … assert 0 == 1` and nothing hints that the environment differs from their shell — which is precisely why the `cyc_LI` cause was misreported twice. The refusal string returned by `run_test_cmd_full` is already carried into the fail envelope as `log_tail` (`juggle_cmd_integrate.py:229` → `_fail(STEP_TEST_FAILURE, _reason, log_tail=_reason)` → `build_envelope`) *and* into the operator-facing action item, so this is the correct, zero-ripple channel. It is deliberately **not** a new structured envelope field: nothing consumes one today (YAGNI), and a general "record the effective env" field would write `~/.juggle/.env` credentials into the DB (Fact 4).

**Files:**
- Modify: `src/juggle_integrate_fullsuite.py`
- Test: `tests/test_integrate_env.py`

**Interfaces:**
- Consumes: `format_env_report`, `dropped_overrides` from `juggle_integrate_env` (Task 2).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_integrate_env.py`:

```python
# A "test_cmd" that always fails, printing one pytest-shaped FAILED line.
DET_FAIL = "sh -c 'echo \"FAILED tests/a.py::t1\"; exit 1'"


def test_failure_reason_names_the_cleared_overrides(tmp_path, monkeypatch):
    """2026-07-25 cyc_LI env-divergence incident: an env-caused divergence must
    ANNOUNCE itself. The refusal (which becomes the action item and the fail
    envelope's log_tail) must name every override integrate cleared, and tell
    the operator how to reproduce integrate's env."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    monkeypatch.setenv("JUGGLE_MAX_THREADS", "10")
    ok, reason = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")

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
    ok, reason = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")

    assert ok is False
    assert "env: sanitized" in reason, reason
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q -k cleared_overrides or env_status`
Expected: **2 failed** — `assert 'JUGGLE_MAX_THREADS=10' in "Tests failed (exit 1) for cyc_probe. No merge performed. stdout tail: FAILED tests/a.py::t1"`.

- [ ] **Step 3: Emit the report in the refusal**

In `src/juggle_integrate_fullsuite.py`, extend the import and the failure branch:

```python
from juggle_integrate_env import dropped_overrides, format_env_report, sanitized_env
```

```python
    env = sanitized_env()
    dropped = dropped_overrides()
    result = _run_once(test_cmd, worktree_path, env)
    if result.returncode != 0:
        # One retry for transient flakes (pilot/Textual tests flake under load).
        result = _run_once(test_cmd, worktree_path, env)
    if result.returncode != 0:
        return False, (
            f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
            f"No merge performed.\n"
            f"{format_env_report(dropped)}\n"
            f"stdout tail: {result.stdout[-300:].strip()}"
        )
    return True, ""
```

> The env line goes **before** the stdout tail deliberately: the tail is the part that gets visually lost, and the env line is the part that answers "why does this pass for me?".

- [ ] **Step 4: Run to verify it passes**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: `7 passed`.

- [ ] **Step 5: LOC gate + full suite**

Run: `cd /tmp/juggle-juggle-LR && python3 scripts/loc_gate.py && make test`
Expected: loc gate `exit=0`; `4331 passed, 7 skipped`.

- [ ] **Step 6: Commit**

```bash
cd /tmp/juggle-juggle-LR
git add src/juggle_integrate_fullsuite.py tests/test_integrate_env.py
git commit -m "fix(integrate): name the cleared env overrides in every test refusal

An env-caused divergence was invisible: the operator saw only 'assert 0 == 1'.
The refusal — which becomes the action item and the fail envelope's log_tail —
now always states which caller overrides integrate cleared and how to reproduce
its env. Only juggle-namespace variables are named, so ~/.juggle/.env
credentials can never reach the DB."
```

---

## Task 4: Make the retry honest — deterministic vs flaky

**Why:** `run_test_cmd_full` retries once "for transient flakes". Against a deterministic env-dependent failure that retry costs a second full ~8-minute suite **and actively misleads** — the operator reasonably reads "it ran twice" as "a flake was ruled out", which is exactly the wrong conclusion.

**Decision: keep the retry, but make it produce the discriminator instead of merely doubling the wait.** Rationale: (a) pilot/Textual flakes under load are real and a spurious red costs a whole repair-dispatch cycle, so removing the retry trades one failure mode for a worse one; (b) running twice is the *only* mechanism that can distinguish deterministic from flaky, so the cost is already being paid — it should buy the answer; (c) the retry only fires on a red, never on the common green path.

A deterministic failure is distinguished from a flake by comparing the **set of failing test node ids** across the two runs — parsed from pytest's default `short test summary info` block (`FAILED <nodeid>` / `ERROR <nodeid>`; verified present under `-q`). Unparseable output degrades to `UNDETERMINED` — never a gate, never an exception.

**Files:**
- Modify: `src/juggle_integrate_fullsuite.py`
- Test: `tests/test_integrate_env.py`

**Interfaces:**
- Produces: `failing_node_ids(stdout: str) -> list[str]`, `retry_verdict(first_stdout: str, second_stdout: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_integrate_env.py`:

```python
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

    ok, reason = run_test_cmd_full(DET_FAIL, str(tmp_path), "cyc_probe")
    assert ok is False
    assert "DETERMINISTIC" in reason, reason
    assert "did NOT rule out a flake" in reason, reason
    assert "tests/a.py::t1" in reason, reason


def test_retry_verdict_calls_differing_failures_flaky(tmp_path):
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason = run_test_cmd_full(FLAKY_FAIL, str(tmp_path), "cyc_probe")
    assert ok is False
    assert "FLAKY-LOOKING" in reason, reason
    assert "tests/a.py::t1" in reason and "tests/b.py::t2" in reason, reason


def test_retry_verdict_is_undetermined_when_output_is_unparseable(tmp_path):
    """Degrade gracefully — a non-pytest test_cmd must never crash the runner."""
    from juggle_integrate_fullsuite import run_test_cmd_full

    ok, reason = run_test_cmd_full("sh -c 'echo boom >&2; exit 3'", str(tmp_path), "cyc_probe")
    assert ok is False
    assert "UNDETERMINED" in reason, reason
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: **4 failed, 7 passed** — the first with `ImportError: cannot import name 'failing_node_ids'`, the others with `assert 'DETERMINISTIC' in "Tests failed (exit 1) …"`.

- [ ] **Step 3: Implement the parser and the verdict**

Add to `src/juggle_integrate_fullsuite.py` (add `import re` at the top):

```python
# pytest's default `short test summary info` block lines (reportchars 'fE').
_SUMMARY_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)")

#: Node ids shown per run in a refusal — enough to act on, short enough to read.
_MAX_NODES_SHOWN = 20


def failing_node_ids(stdout: str) -> list[str]:
    """Sorted, de-duplicated test node ids from a pytest run's short summary.

    Empty for a non-pytest ``test_cmd`` or output without a summary block —
    the caller degrades to UNDETERMINED rather than guessing.
    """
    found = set()
    for line in stdout.splitlines():
        m = _SUMMARY_LINE.match(line.strip())
        if m:
            found.add(m.group(1))
    return sorted(found)


def _show(nodes: list[str]) -> str:
    shown = ", ".join(nodes[:_MAX_NODES_SHOWN])
    extra = len(nodes) - _MAX_NODES_SHOWN
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def retry_verdict(first_stdout: str, second_stdout: str) -> str:
    """Was the doubled suite run a flake check, or a confirmation of a real red?

    The retry exists for transient flakes, but against a DETERMINISTIC failure
    it merely doubles an ~8-minute wait while implying a flake was ruled out
    (2026-07-25 cyc_LI incident). Comparing the two runs' failing sets turns
    that wasted wall-clock into the answer the operator actually needs.
    """
    first, second = failing_node_ids(first_stdout), failing_node_ids(second_stdout)
    if not first or not second:
        return (
            "retry: UNDETERMINED — no pytest failure summary could be parsed from "
            "the output, so running twice ruled nothing out."
        )
    if first == second:
        return (
            f"retry: DETERMINISTIC — the SAME {len(first)} test(s) failed on BOTH "
            f"runs, so the retry did NOT rule out a flake, it CONFIRMED a real "
            f"red: {_show(first)}"
        )
    return (
        "retry: FLAKY-LOOKING — the two runs failed DIFFERENT tests. "
        f"run 1: {_show(first)} | run 2: {_show(second)}"
    )
```

- [ ] **Step 4: Wire it into `run_test_cmd_full`**

```python
    env = sanitized_env()
    dropped = dropped_overrides()
    result = _run_once(test_cmd, worktree_path, env)
    if result.returncode != 0:
        # One retry — for transient flakes, AND to discriminate them from a
        # deterministic red (retry_verdict compares the two failing sets).
        first_stdout = result.stdout
        result = _run_once(test_cmd, worktree_path, env)
        if result.returncode != 0:
            return False, (
                f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
                f"No merge performed.\n"
                f"{format_env_report(dropped)}\n"
                f"{retry_verdict(first_stdout, result.stdout)}\n"
                f"stdout tail: {result.stdout[-300:].strip()}"
            )
    return True, ""
```

- [ ] **Step 5: Run to verify they pass**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: `11 passed`.

- [ ] **Step 6: LOC gate + full suite**

Run: `cd /tmp/juggle-juggle-LR && python3 scripts/loc_gate.py && wc -l src/juggle_integrate_fullsuite.py && make test`
Expected: loc gate `exit=0`; `juggle_integrate_fullsuite.py` **under 300 lines** (~155 projected — if it exceeds 300, STOP and extract `failing_node_ids`/`retry_verdict` into `src/juggle_integrate_retry.py` as a separate refactor commit first); `4335 passed, 7 skipped`.

- [ ] **Step 7: Commit**

```bash
cd /tmp/juggle-juggle-LR
git add src/juggle_integrate_fullsuite.py tests/test_integrate_env.py
git commit -m "fix(integrate): make the suite retry discriminate flake from deterministic red

The retry existed for transient flakes, but against a deterministic failure it
doubled an ~8-minute wait AND implied a flake had been ruled out. It now
compares the two runs' failing node-id sets and says which it was:
DETERMINISTIC / FLAKY-LOOKING / UNDETERMINED. The retry stays — running twice
is the only way to tell, and it now buys the answer instead of just time."
```

---

## Task 5: `make test` parity — one documented command reproduces integrate's env

**Why:** the whole failure mode is "passes for me, fails in integrate". Parity has to be *mechanical*, not documented-by-convention: the local command must go through the **same `sanitized_env()` function**, and a pin must fail if the Makefile ever stops routing through it.

**Files:**
- Create: `scripts/run_integrate_env.py`
- Modify: `Makefile`, `docs/ARCHITECTURE.md`
- Test: `tests/test_integrate_env.py`

**Interfaces:**
- Consumes: `juggle_integrate_env.sanitized_env`.
- Produces: `scripts/run_integrate_env.py <command> [args...]` — execs the command under integrate's env. `--print-cleared` lists the variables it would clear (`--json` for machine output).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_integrate_env.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q -k run_integrate_env or make_test_integrate`
Expected: **2 failed** — the first with a non-zero return code (`can't open file 'scripts/run_integrate_env.py'`), the second with `AssertionError: Makefile must expose a test-integrate target`.

- [ ] **Step 3: Create `scripts/run_integrate_env.py`**

```python
#!/usr/bin/env python3
"""Run a command under EXACTLY the environment `juggle integrate` gives the suite.

`make test-integrate` is this script. It exists so a human or agent can
reproduce integrate's verdict locally with one command, through the SAME
`juggle_integrate_env.sanitized_env()` the integrate gate uses — never a
hand-copied list of variables that can drift (2026-07-25 cyc_LI
env-divergence incident: 4 failed integrations, 8 commits blocked ~3 days,
because the suite silently saw a different environment than the operator did).

Usage:
    run_integrate_env.py <command> [args...]   run <command> under integrate's env
    run_integrate_env.py --print-cleared       list the variables it would clear
    run_integrate_env.py --print-cleared --json

Stdlib-only; safe to run with any python3 (the wrapped command does its own
`uv run` if it needs the project venv).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_integrate_env import dropped_overrides, sanitized_env  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2

    if argv[0] == "--print-cleared":
        cleared = dropped_overrides()
        if "--json" in argv:
            print(json.dumps(cleared, indent=2, sort_keys=True))
        else:
            for name in sorted(cleared):
                print(f"{name}={cleared[name]}")
        return 0

    try:
        os.execvpe(argv[0], argv, sanitized_env())
    except OSError as e:  # command not found / not executable
        print(f"run_integrate_env: cannot run {argv[0]!r}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Make it executable: `chmod +x scripts/run_integrate_env.py`

- [ ] **Step 4: Add the Makefile target**

Add to `Makefile`, updating the `.PHONY` line to `.PHONY: test test-fast test-integrate`:

```make
# EXACT parity with the integrate gate: the same FULL suite under the same
# sanitized environment integrate uses (juggle's own JUGGLE_*/_JUGGLE_*/
# CLAUDE_PLUGIN_DATA overrides cleared, everything else passed through).
# Routes through scripts/run_integrate_env.py -> juggle_integrate_env.sanitized_env,
# so "passes for me" and "passes in integrate" cannot silently diverge
# (2026-07-25 cyc_LI env-divergence incident). Use this to reproduce an
# integrate test failure locally.
test-integrate:
	python3 scripts/run_integrate_env.py uv run pytest -n auto --dist loadgroup -m "not watchdog_proc"
```

> `python3`, not `uv run python`: the wrapper is stdlib-only and must set the environment in the **parent** so the `uv run pytest` it execs inherits it. Portable on macOS and Debian.

- [ ] **Step 5: Run to verify the tests pass**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py -q`
Expected: `13 passed`.

- [ ] **Step 6: Verify the target end-to-end, and that it matches `make test`**

Run:
```bash
cd /tmp/juggle-juggle-LR
python3 scripts/run_integrate_env.py --print-cleared
JUGGLE_MAX_THREADS=10 make test-integrate 2>&1 | tail -3
```
Expected: `--print-cleared` lists this shell's juggle overrides (one `NAME=value` per line, possibly none). `make test-integrate` ends with a summary line of the form `N passed, 7 skipped in …s` and `N` matches the count `make test` reports — the two differ only in environment, and after this plan they must not differ in outcome.

- [ ] **Step 7: Document the contract in `docs/ARCHITECTURE.md`**

Insert immediately after the `### LOC gate` section:

```markdown
### Integrate test environment

`integrate` decides merge/no-merge by running the repo's configured `test_cmd`, so that run must
be environment-deterministic: the same commit on the same branch must produce the same verdict
whichever caller invoked integrate (watchdog daemon, operator shell, agent pane — their
environments differ, and the CLI path additionally loads `~/.juggle/.env`).

`src/juggle_integrate_env.py` owns the contract. Integrate **CLEARS** juggle's own namespace —
`JUGGLE_*`, `_JUGGLE_*`, `CLAUDE_PLUGIN_DATA` — and **passes everything else through** (`HOME`,
`PATH`, `TMPDIR`, `UV_*`, `GIT_*`, …). A deny-list, not an allow-list: the set juggle owns is small
and knowable, the set the toolchain needs is not. Values are **cleared, never pinned** — pinning
would keep the ambient coupling alive and could turn a false red into a false green.

Consequence for test authors: **a test may not read a `JUGGLE_*` variable ambiently.** Pin what you
need with `monkeypatch.setenv`, as `tests/conftest.py` already does for `JUGGLE_DB_PATH`,
`_JUGGLE_CONFIG_PATH`, `JUGGLE_SPOOL_DIR` and `JUGGLE_ORCHESTRATOR`.

Reproduce integrate's exact environment locally with **`make test-integrate`** — it runs the full
suite through the same `sanitized_env()` code path. Every test failure integrate reports also names
the overrides it cleared, and states whether the retry found the failure deterministic or flaky.

(2026-07-25 `cyc_LI` incident: the suite inherited the caller's env, a test read the ambient
`JUGGLE_MAX_THREADS=10` that operator shells export and the watchdog does not — 4 failed
integrations, 8 commits blocked ~3 days.)
```

- [ ] **Step 8: Full suite + LOC gate**

Run: `cd /tmp/juggle-juggle-LR && python3 scripts/loc_gate.py && make test`
Expected: loc gate `exit=0`; `4337 passed, 7 skipped`.

- [ ] **Step 9: Commit**

```bash
cd /tmp/juggle-juggle-LR
git add scripts/run_integrate_env.py Makefile docs/ARCHITECTURE.md tests/test_integrate_env.py
git commit -m "feat(integrate): make test-integrate reproduces integrate's exact suite env

One documented command runs the full suite under the same sanitized environment
the integrate gate uses, through the same sanitized_env() code path — plus a pin
that fails if the Makefile or the wrapper ever stops routing through it. Closes
the 'passes for me, fails in integrate' gap that cost cyc_LI 4 integrations."
```

- [ ] **Step 10: Refresh the graph**

Run: `cd /tmp/juggle-juggle-LR && graphify update .`

---

## Task 6 (OPTIONAL — needs sign-off, see Open Question 2): key red-suite repair signatures on the failing tests

**Adjacent defect found while grounding this plan, enabled by Task 4 — ship separately or not at all.**

`juggle_integrate_envelope.compute_signature(fail_class, files_or_tests)` fingerprints a failure so `check_retry_policy` can cap repairs at 1 per signature. But `juggle_cmd_integrate.py:230` calls `_fail(STEP_TEST_FAILURE, _reason, log_tail=_reason)` with **no `files=`** — so *every* red-suite failure of any topic hashes to `sha1("red-suite|")`, one constant. The per-signature cap therefore cannot distinguish "the same test failed again" from "a completely different test failed", and a topic burns its cap on unrelated reds. Task 4's `failing_node_ids` is exactly the missing fingerprint.

**Why it is optional:** it changes repair-dispatch and escalation behaviour (distinct failing sets now each get their own attempt, still bounded by the 3-per-topic backstop) — beyond the brief's scope, and behaviour the user did not ask for.

**Files:** `src/juggle_integrate_fullsuite.py`, `src/juggle_cmd_integrate.py:227-231`, `tests/test_integrate.py:512`, `tests/test_integrate_env.py`

- [ ] **Step 1: Write the failing test**

```python
def test_red_suite_signature_differs_per_failing_test_set():
    """Adjacent to the 2026-07-25 cyc_LI incident: red-suite failures all hashed
    to sha1('red-suite|') because integrate passed no files= to _fail, so the
    per-signature repair cap could not tell two different reds apart."""
    from juggle_integrate_envelope import RED_SUITE, compute_signature

    assert compute_signature(RED_SUITE, ["tests/a.py::t1"]) != compute_signature(
        RED_SUITE, ["tests/b.py::t2"]
    )
    assert compute_signature(RED_SUITE, []) == compute_signature(RED_SUITE, [])
```

- [ ] **Step 2: Widen the runner's return to carry the node ids**

`run_test_cmd_full` returns `tuple[bool, str, list[str]]` — `(ok, reason, failing_nodes)`; `failing_nodes` is `failing_node_ids(result.stdout)` on failure, `[]` otherwise.

- [ ] **Step 3: Update the two call sites**

`src/juggle_cmd_integrate.py`:

```python
            _ok, _reason, _failing = run_test_cmd_full(test_cmd, worktree_path, worktree_branch)
            if not _ok:
                return _fail(STEP_TEST_FAILURE, _reason, log_tail=_reason, files=_failing)
```

`tests/test_integrate.py:512` — the `fake_suite` double:

```python
    def fake_suite(test_cmd, worktree_path, worktree_branch):
        lp = captured.get("lock_path")
        captured["held_during_suite"] = bool(lp and lp.exists())
        return False, "stop-after-lock-check", []  # short-circuit the rest
```

- [ ] **Step 4: Verify + full suite + commit**

Run: `cd /tmp/juggle-juggle-LR && uv run pytest tests/test_integrate_env.py tests/test_integrate.py tests/test_integrate_envelope.py -q && make test`
Expected: all pass; `4338 passed, 7 skipped`.

```bash
git commit -m "fix(integrate): key red-suite repair signatures on the failing test ids

Every red-suite failure hashed to sha1('red-suite|') because integrate passed no
files= to _fail, so the per-signature repair cap could not distinguish two
different reds. The failing node ids parsed for the retry verdict are now the
fingerprint. The 3-repairs-per-topic backstop is unchanged."
```

---

## Devil's Advocate

Written against this plan, not for it. Each item: weakest assumption → failure mode → mitigation.

**1. "What legitimately breaks if the env is sanitized?"**
*Assumption:* no test genuinely needs an ambient `JUGGLE_*` or `CLAUDE_PLUGIN_DATA`.
*Evidence, not hope:* the FULL suite was run on this branch with those variables stripped — `4324 passed, 7 skipped`. `conftest.py` already `monkeypatch.setenv`s every juggle variable the suite depends on (`JUGGLE_DB_PATH`, `_JUGGLE_CONFIG_PATH`, `JUGGLE_CONFIG_DIR`, `JUGGLE_SPOOL_DIR`, `JUGGLE_ORCHESTRATOR`, `JUGGLE_WATCHDOG_DISABLE_SPAWN`), and every test that wants `CLAUDE_PLUGIN_DATA` sets it itself. `src/juggle_db.py` and `src/juggle_cli.py` read none of the variables `CLAUDE.md` claims are required.
*Residual failure mode:* thread `LI` is concurrently editing tests; a test landing after this measurement could reintroduce an ambient read. It would then be red for **everyone** — integrate, `make test`, and `make test-integrate` alike — which is the correct, visible outcome, not a silent divergence. `tests/watchdog/test_never_tasked.py:38-39` sets `os.environ["JUGGLE_MAX_*"]` directly (no monkeypatch, no cleanup) — it *writes* rather than reads, so sanitization does not affect it, but it is a leak worth `LI`'s sweep.

**2. "Could this make integrate PASS something that should fail — a false green, strictly worse than today's false red?"**
This is the sharpest objection and it is **why option 1 (pinning) was rejected.** Pinning `JUGGLE_MAX_THREADS=10` would have made the `cyc_LI` test go green in integrate while remaining broken on a fresh checkout — a merged-broken-code outcome.
Sanitizing cannot invent a passing condition: it only *removes* overrides, so the suite runs against `DEFAULTS` + `config.json` — precisely a fresh checkout. `test_sanitized_env_never_pins_a_value` pins this property.
*The one residual false-green shape:* a test written as `if os.environ.get("JUGGLE_X"): assert <something real>` would pass vacuously once `JUGGLE_X` is cleared. No such test exists today (verified by scanning `tests/` for ambient `os.environ` reads of juggle variables — the only hits read `JUGGLE_DB_PATH`, which conftest pins). And crucially, `make test-integrate` uses the **identical** env, so such a test would be vacuous everywhere equally — a suite-design bug visible to every developer, not a divergence between two environments. Determinism is the goal; omniscience is not.

**3. "What about tests that read `HOME`, `TMPDIR`, `PATH`, or the real `~/.claude/juggle` DB?"**
They keep working: none of those is in the deny-list. `HOME` in particular must pass through — `uv`'s cache, `git`'s config, and dozens of `Path.home()` call sites need it, and a fabricated `HOME` would break the toolchain far more thoroughly than the bug being fixed. Protection against the *real* DB is orthogonal and already fail-closed: `conftest._isolate_db_from_prod` raises on any prod-DB `_connect`, `_isolate_config_from_prod` raises on any prod-config write, `_isolate_spool_from_prod` guards the spool. This plan does not weaken those, and it does not try to duplicate them.
*Newly noted, deliberately out of scope:* `~/.juggle/.env` credentials reach the suite on the CLI path but not the daemon path. That is the same divergence class, but stripping credentials could flip network-touching tests from skipped to executing (or vice versa) — a change with its own blast radius. Recorded as Open Question 3, not silently bundled.

**4. "Is refusing (option 3) better than normalizing, since a refusal is loud and a silent normalization is not?"**
Refusing is loud but *misaimed*: the watchdog — which runs most integrations — has a clean env and would never trip the refusal, while every operator shell and agent pane (which do export `JUGGLE_MAX_THREADS`, because the repo's own boilerplate says to) would be blocked. It would halt integrate broadly while making zero verdicts more deterministic. So refusal is rejected as the *mechanism*.
But the objection's premise is right, and the plan concedes it: **the normalization is not silent.** Task 3 emits the exact cleared set — names *and* values — in every test-failure refusal, which flows into the operator's action item and the fail envelope's `log_tail`, together with the reproduction command. The loudness is placed where it helps (on failure) rather than where it only obstructs (on every invocation).

**5. "The diagnostic goes into a prose string, not a structured envelope field."**
*Weakest point of Task 3.* A structured `envelope["env_sanitized"]` would be machine-readable for the watchdog. It was rejected for two reasons: nothing consumes such a field today (YAGNI), and the generalized version the brief hints at — "record the effective env" — would serialize `OPENROUTER_KEY` / `VOYAGE_API_KEY` / `DEEPSEEK_API_KEY` into the DB and into action items, since `juggle_cli` loads `~/.juggle/.env` into `os.environ` at import. The chosen channel (`reason` → `log_tail` → envelope) reaches both the human and the envelope with zero ripple, and `test_env_report_never_leaks_a_non_controlled_variable` pins the redaction property. If a machine consumer ever appears, promoting the report to a field is a small, additive change.

**6. "Node-id parsing is fragile — pytest could change its summary format."**
True, and it is why `retry_verdict` **never gates**: an unparseable output yields `UNDETERMINED` and the merge decision is unchanged (still a refusal, driven solely by the exit code). The format was verified empirically under this repo's own `-q` invocation. Worst case the operator loses one advisory line.

**7. "Keeping the retry still doubles an 8-minute wait on every red."**
Conceded — the wall-clock cost is real and unchanged. What changes is that the second run now *buys* something: the deterministic/flaky verdict, which is otherwise unobtainable. Dropping the retry would save 8 minutes per red at the cost of dispatching repair agents at genuine flakes (the pilot/Textual tests this repo documents as load-flaky), which costs far more than 8 minutes. If the cost later proves unjustified, the change is a two-line revert in one function — the verdict logic is already isolated in `retry_verdict`.

**8. "`_run_once(env=dict(os.environ))` in Task 1 is claimed behaviour-preserving."**
On POSIX, `env=None` and `env=dict(os.environ)` hand the child the same mapping; the difference is Windows-only (`SYSTEMROOT`), and this project targets macOS + Debian. Task 1's gate is the existing `test_integrate.py` + `test_marker_tier.py` suite, including the grep-pin for the literal `test_cmd, shell=True`, which `_run_once` preserves.

**9. "`make test` and `make test-integrate` are now two commands — the wrong one will get run."**
Real ergonomic risk. Mitigated by making the *documented* reproduction path the parity one (`docs/ARCHITECTURE.md`, the refusal message itself, and the Makefile comment all point at `make test-integrate`), and by the parity pin that fails if the wiring ever diverges. The stronger fix — sanitizing `make test` itself so there is only one command — is Open Question 4; it is a broader behaviour change to every contributor's inner loop and should be the user's call, not smuggled in here.

---

## Open Questions

Batched, not blocking — every task above is executable as written.

1. **`CLAUDE.md`'s stale "Required environment variables" block.** It states `CLAUDE_PLUGIN_DATA (juggle_cli.py)` and `JUGGLE_MAX_BACKGROUND_AGENTS, JUGGLE_MAX_THREADS (juggle_db.py)` are required with no defaults, and its Testing section tells every contributor and agent to `export JUGGLE_MAX_THREADS=10` before running the suite. All three claims are false (verified: neither module reads them; the full suite is green without them) — **and that boilerplate is the proximate cause of the `cyc_LI` incident**, since it is why every human run had the variable and the watchdog did not. This plan is forbidden from editing `CLAUDE.md`. Should a follow-up task remove that block and repoint the Testing section at `make test` / `make test-integrate`? *Recommendation: yes, urgently — the fix here is incomplete while the docs still teach the habit.*
2. **Task 6 (red-suite repair signatures).** Ship it? It fixes a real defect — every red-suite failure currently shares one signature, so the per-signature repair cap cannot distinguish two different reds — but it alters repair-dispatch/escalation behaviour beyond this brief's scope. *Recommendation: ship, as its own commit, after Tasks 1–5 have landed and been observed for a few integrations.*
3. **`~/.juggle/.env` credentials in the suite env.** The CLI path loads `OPENROUTER_KEY` / `VOYAGE_API_KEY` / `DEEPSEEK_API_KEY` into `os.environ`; the watchdog daemon path does not. That is the same divergence class this plan fixes, but for secrets. Should integrate also clear them (making the suite hermetic w.r.t. credentials), or would that flip network-touching tests between skipped and executing? *Recommendation: leave as-is for now; today's tests pin these via `monkeypatch`, so there is no live divergence — but it belongs on the list.*
4. **Should `make test` itself be sanitized**, collapsing `test` and `test-integrate` into one command so the wrong one cannot be run? *Recommendation: yes eventually — it removes the ergonomic footgun entirely — but it changes every contributor's inner loop, so it wants an explicit decision rather than a side effect of this plan.*
5. **Should `juggle doctor` warn** when the invoking shell has `JUGGLE_*` overrides set that integrate will clear? Cheap (`dropped_overrides()` already exists) and would surface the mismatch before an 8-minute suite run rather than after. *Recommendation: nice-to-have, not needed for correctness.*

---

## Self-Review

- **Brief coverage.** Environment-determinism → Tasks 1–2. Diagnosability → Task 3 (+ the redaction pin). Retry decision → Task 4 (kept, with an explicit deterministic/flaky discriminator). Which variables integrate controls vs passes through, and why → the "env contract" section, the module docstring, and `docs/ARCHITECTURE.md`. `make test` parity in one documented command → Task 5. Refactoring-first → Task 1 (behaviour-preserving, LOC gate checked: 72 lines today, ~155 projected, limit 300). Devil's advocate → all five prompted questions answered above, plus four self-identified weaknesses. Ordered, independently-shippable steps with per-step RED-first tests and greppable agent-verifiable gates → every task.
- **Headline gate is explicit:** `test_integrate_verdict_is_invariant_to_ambient_juggle_env` — a `test_cmd` that reads an ambient `JUGGLE_*` variable yields the same integrate verdict with and without the caller's export. Deterministic, ~0.2s, no pytest-in-pytest.
- **Fixtures verified by execution, not assumed:** the `CANARY`, `DET_FAIL` and `FLAKY_FAIL` shell commands were run against `subprocess.run(shell=True)` before being written into this plan (canary `rc=0` sanitized / `rc=1` polluted; `DET_FAIL` identical output twice; `FLAKY_FAIL` `tests/a.py::t1` then `tests/b.py::t2`). pytest's `FAILED <nodeid>` summary line was verified present under `-q`.
- **Type/name consistency:** `sanitized_env` / `dropped_overrides` / `format_env_report` / `is_controlled` (Task 2) are used under those exact names in Tasks 3, 5 and the wrapper script; `failing_node_ids` / `retry_verdict` (Task 4) likewise in Task 6. `run_test_cmd_full` keeps its `(bool, str)` arity through Task 5 and widens to `(bool, str, list[str])` only in Task 6, where both call sites (`juggle_cmd_integrate.py` and the `fake_suite` double at `tests/test_integrate.py:512`) are updated in the same step.
- **No placeholders:** every step contains the actual code, the exact command, and the expected output.
