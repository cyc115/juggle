# Watchdog Sole Reaper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a race-condition bug in `reap_stale_agents` Pass 2, make the cockpit read-only, and consolidate all periodic reaping under the watchdog.

**Architecture:** Four separately committable subtasks (A→D), each independently testable. A closes the Pass 2 boot-grace race (bug fix). B removes state mutation from the cockpit's read path (smell fix). C makes the watchdog the sole periodic reaper and removes the two non-mutation callers. D adds a watchdog heartbeat so CLI commands can detect a dead watchdog and warn/recover instead of silently skipping reaps.

**Tech Stack:** Python 3.12, pytest, `unittest.mock`, `juggle_tmux.py`, `juggle_cockpit.py`, `juggle_cli.py`, `juggle_cmd_agents.py`, `scripts/juggle-agent-watchdog`

**Branch:** `cyc_watchdog-sole-reaper` — never land on main directly; do NOT hot-reload into a running juggle session during implementation.

---

## File Map

| File | Change |
|------|--------|
| `src/juggle_tmux.py` | Add `_get_pane_start_time()` helper; add boot-grace check in Pass 2 (lines 520–539) |
| `src/juggle_cockpit.py` | Remove throttled-reaper block (lines 361–367); remove `_last_reap` init (line 229); keep `_cockpit_mgr` (used by other actions) |
| `src/juggle_cli.py` | Remove reap block (lines 904–913) |
| `src/juggle_cmd_agents.py` | Remove `reap_stale_agents` call (lines 538–541) |
| `scripts/juggle-agent-watchdog` | Add `reap_stale_agents(db, mgr)` at end of `_poll_once`; write heartbeat file per poll |
| `src/juggle_watchdog_health.py` | New file: `is_watchdog_alive(stale_secs) -> bool`, `write_heartbeat()`, `HEARTBEAT_PATH` |
| `tests/test_reaper.py` | Add Task A pass-2-grace tests |
| `tests/test_cockpit_readonly.py` | New: assert `reap_stale_agents` never called from `_refresh` |
| `tests/test_watchdog_health.py` | New: heartbeat write/read/stale tests |

---

## Task A: Pass 2 Boot Grace

**Files:**
- Modify: `src/juggle_tmux.py:520-539`
- Test: `tests/test_reaper.py`

The race: `spawn_pane()` at line 400 → `start_claude_in_pane()` at line 401 sets `JUGGLE_IS_AGENT=1` → `db.create_agent()` at line 403. Pass 2 kills any `JUGGLE_IS_AGENT=1` pane not in the DB immediately — which hits panes in this window. Fix: read `#{pane_start_time}` from tmux and skip panes younger than `cold_start_grace`.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_reaper.py`:

```python
def test_pass2_skips_new_pane_within_boot_grace():
    """Pass 2 must not kill a pane younger than boot grace (race window fix)."""
    import subprocess
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"

    # No DB record for this pane — it's in the race window
    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    # pane_start_time = now (just created)
    import time
    fresh_start = int(time.time())

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-new\n"
        elif "display-message" in cmd:
            r.returncode = 0
            r.stdout = str(fresh_start)
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0, f"should not reap pane within grace, got {reaped}"
    mock_mgr.kill_pane.assert_not_called()


def test_pass2_kills_old_orphan_pane_past_grace():
    """Pass 2 must kill a pane older than boot grace with no DB record."""
    import subprocess
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"

    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    import time
    old_start = int(time.time()) - 300  # 5 minutes old — well past 120s grace

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-old\n"
        elif "display-message" in cmd:
            r.returncode = 0
            r.stdout = str(old_start)
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 1, f"should reap old orphan pane, got {reaped}"
    mock_mgr.kill_pane.assert_called_once_with("%pane-old")


def test_pass2_skips_grace_when_pane_start_time_unavailable():
    """Pass 2 is conservative: if pane age can't be read, skip (don't kill)."""
    import subprocess
    from juggle_tmux import reap_stale_agents

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_mgr.session_name = "juggle"

    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    def fake_run(cmd, **kwargs):
        r = mock.MagicMock()
        if "list-panes" in cmd:
            r.returncode = 0
            r.stdout = "%pane-unknown\n"
        elif "display-message" in cmd:
            r.returncode = 1  # tmux error — can't read start time
            r.stdout = ""
        else:
            r.returncode = 1
            r.stdout = ""
        return r

    with mock.patch("subprocess.run", side_effect=fake_run):
        with mock.patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            reaped = reap_stale_agents(mock_db, mock_mgr)

    assert reaped == 0, "should not kill when pane age is unreadable"
    mock_mgr.kill_pane.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/mikechen/github/juggle
uv run pytest tests/test_reaper.py::test_pass2_skips_new_pane_within_boot_grace \
               tests/test_reaper.py::test_pass2_kills_old_orphan_pane_past_grace \
               tests/test_reaper.py::test_pass2_skips_grace_when_pane_start_time_unavailable \
               -v
```

Expected: 3 FAIL (kill_pane called when it shouldn't be, or not called when it should be)

- [ ] **Step 3: Add `_get_pane_start_time` helper**

In `src/juggle_tmux.py`, add before `reap_stale_agents` (around line 458):

```python
def _get_pane_start_time(pane_id: str) -> float | None:
    """Return Unix timestamp when the pane was created, or None on failure."""
    import subprocess as _sp
    try:
        r = _sp.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_start_time}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None
```

- [ ] **Step 4: Add grace check to Pass 2**

In `src/juggle_tmux.py`, replace the Pass 2 kill block (lines 535–537):

```python
# Before:
            if _pane_has_juggle_agent_env(pane_id):
                mgr.kill_pane(pane_id)
                reaped += 1

# After:
            if _pane_has_juggle_agent_env(pane_id):
                pane_start = _get_pane_start_time(pane_id)
                if pane_start is None:
                    continue  # conservative: skip if age unreadable
                import time as _time
                if _time.time() - pane_start < cold_start_grace:
                    continue  # within boot grace — skip
                mgr.kill_pane(pane_id)
                reaped += 1
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run pytest tests/test_reaper.py -v
```

Expected: all pass (including the 3 new + all existing)

- [ ] **Step 6: Commit**

```bash
git add src/juggle_tmux.py tests/test_reaper.py
git commit -m "fix(reaper): add boot grace to Pass 2 orphan-pane kill

Pass 2 previously killed any JUGGLE_IS_AGENT=1 pane with no DB record
immediately, including panes in the spawn_pane→db.create_agent race window.
Now reads #{pane_start_time} and skips panes younger than agent_boot_grace_secs.
Conservative: if pane age is unreadable, skip rather than kill."
```

---

## Task B: Cockpit Read-Only

**Files:**
- Modify: `src/juggle_cockpit.py`
- Test: `tests/test_cockpit_readonly.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/test_cockpit_readonly.py`:

```python
"""Assert that CockpitApp._refresh() never calls reap_stale_agents."""
import sys
from pathlib import Path
from unittest import mock

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_refresh_does_not_call_reap_stale_agents():
    """_refresh must not mutate agent state — cockpit is display-only."""
    # Patch heavy dependencies before import
    with mock.patch.dict("sys.modules", {
        "textual": mock.MagicMock(),
        "textual.app": mock.MagicMock(),
        "textual.binding": mock.MagicMock(),
        "textual.containers": mock.MagicMock(),
        "textual.widgets": mock.MagicMock(),
        "textual.events": mock.MagicMock(),
        "juggle_cockpit_model": mock.MagicMock(),
        "juggle_cockpit_view": mock.MagicMock(),
        "juggle_cockpit_helpers": mock.MagicMock(),
        "juggle_db": mock.MagicMock(),
        "juggle_settings": mock.MagicMock(),
        "rich": mock.MagicMock(),
    }):
        import importlib
        import juggle_cockpit
        importlib.reload(juggle_cockpit)

        app = juggle_cockpit.CockpitApp.__new__(juggle_cockpit.CockpitApp)
        app._db = mock.MagicMock()
        app._offsets = {}
        app._active_pane = "notifications"
        app._filter = {"actions": "", "agents": "", "notifications": ""}
        app._prev_action_ids = set()
        app._prev_agent_statuses = {}
        app._bell_enabled = False
        app._desktop_notif_enabled = False

        with mock.patch("juggle_tmux.reap_stale_agents") as mock_reap:
            # Force many refresh calls (previously, _last_reap=0 meant it fired immediately)
            for _ in range(5):
                try:
                    app._refresh()
                except Exception:
                    pass  # snapshot/render errors are fine — we only check reap

        mock_reap.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_cockpit_readonly.py -v
```

Expected: FAIL — `mock_reap` is called because `_refresh` currently contains the reap block.

- [ ] **Step 3: Remove the throttled-reaper block from `_refresh`**

In `src/juggle_cockpit.py`, remove lines 359–367 (the full throttled-reap block):

```python
# Remove this entire block:
            # Throttled reaper (60s)
            now = time.time()
            if now - self._last_reap >= 60 and self._cockpit_mgr is not None:
                try:
                    from juggle_tmux import reap_stale_agents
                    reap_stale_agents(self._db, self._cockpit_mgr)
                    self._last_reap = now
                except Exception:
                    pass
```

Also remove the `_last_reap` initialization from `__init__` (line 229):

```python
# Remove:
        self._last_reap: float = 0.0
```

Note: `_cockpit_mgr` and its initialization (lines 246–251) must stay — it is used by other action handlers in the cockpit.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_cockpit_readonly.py -v
```

Expected: PASS

- [ ] **Step 5: Run full suite to check for regressions**

```bash
uv run pytest -q
```

Expected: same pass count as before Task B (minus any test that explicitly asserted reaping from cockpit, which there should be none).

- [ ] **Step 6: Commit**

```bash
git add src/juggle_cockpit.py tests/test_cockpit_readonly.py
git commit -m "fix(cockpit): remove reap_stale_agents from _refresh — cockpit is read-only

Dashboard components must not perform destructive operations. Runtime behavior
(which agents die and when) must not depend on whether the cockpit is open.
Periodic reaping is now the watchdog's sole responsibility (Task C)."
```

---

## Task C: Watchdog Sole Reaper

**Files:**
- Modify: `scripts/juggle-agent-watchdog` (add reap to `_poll_once`)
- Modify: `src/juggle_cli.py` (remove reap block)
- Modify: `src/juggle_cmd_agents.py` (remove reap call)
- Test: `tests/test_watchdog_reaper.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/test_watchdog_reaper.py`:

```python
"""Assert that the watchdog's _poll_once calls reap_stale_agents."""
import sys
from pathlib import Path
from unittest import mock
import importlib

SCRIPTS_DIR = str(Path(__file__).parent.parent / "scripts")
SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _load_watchdog_script():
    """Load juggle-agent-watchdog as a module (hyphen in name requires spec load)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "juggle_agent_watchdog",
        Path(__file__).parent.parent / "scripts" / "juggle-agent-watchdog",
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch heavy imports before exec
    with mock.patch.dict("sys.modules", {
        "juggle_db": mock.MagicMock(),
        "juggle_settings": mock.MagicMock(),
        "juggle_tmux": mock.MagicMock(),
        "juggle_watchdog": mock.MagicMock(),
    }):
        spec.loader.exec_module(mod)
    return mod


def test_poll_once_calls_reap_stale_agents():
    """_poll_once must call reap_stale_agents so watchdog is sole periodic reaper."""
    mod = _load_watchdog_script()

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_db.get_all_agents.return_value = []
    mock_db.get_current_thread.return_value = "t1"

    with mock.patch("juggle_tmux.reap_stale_agents") as mock_reap:
        # Patch module-level imports used inside _poll_once
        with mock.patch.object(mod, "check_orphaned_threads"):
            with mock.patch.object(mod, "get_session_id", return_value="s1"):
                mod._poll_once(mock_db, mock_mgr)

    mock_reap.assert_called_once_with(mock_db, mock_mgr)


def test_cli_main_does_not_call_reap_stale_agents():
    """juggle_cli main() must not call reap_stale_agents — watchdog owns periodic reap."""
    import ast
    cli_path = Path(__file__).parent.parent / "src" / "juggle_cli.py"
    source = cli_path.read_text()
    # Simple AST check: no top-level reap_stale_agents call outside a function def
    # (the call at module level in main() is what we're removing)
    tree = ast.parse(source)
    # Find the main() function body
    main_func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"),
        None,
    )
    assert main_func is not None
    # Look for any Call to reap_stale_agents in main body (direct or in try/except)
    reap_calls = [
        n for n in ast.walk(main_func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "reap_stale_agents"
    ]
    assert len(reap_calls) == 0, (
        f"main() still calls reap_stale_agents {len(reap_calls)} time(s) — remove it"
    )


def test_cmd_agents_get_agent_does_not_call_reap():
    """cmd_get_agent must not call reap_stale_agents — watchdog owns periodic reap."""
    import ast
    path = Path(__file__).parent.parent / "src" / "juggle_cmd_agents.py"
    source = path.read_text()
    tree = ast.parse(source)
    # cmd_get_agent is the function that called reap
    func = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_get_agent"),
        None,
    )
    assert func is not None
    reap_calls = [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "reap_stale_agents"
    ]
    assert len(reap_calls) == 0, (
        f"cmd_get_agent still calls reap_stale_agents {len(reap_calls)} time(s) — remove it"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_watchdog_reaper.py -v
```

Expected: `test_poll_once_calls_reap_stale_agents` FAIL (not called), `test_cli_main_does_not_call_reap_stale_agents` FAIL (call found in AST), `test_cmd_agents_get_agent_does_not_call_reap` FAIL (call found in AST).

- [ ] **Step 3: Add `reap_stale_agents` call to `_poll_once` in watchdog script**

In `scripts/juggle-agent-watchdog`, at the end of `_poll_once` (after the `check_orphaned_threads` call at line 195):

```python
    # Loop 2: orphaned thread detection
    _orphan_threshold = float(os.environ.get("JUGGLE_ORPHAN_THRESHOLD", "300"))
    check_orphaned_threads(db, orphan_threshold=_orphan_threshold)

    # Generic stale-agent reap (pass 1: idle TTL / dead panes; pass 2: orphan panes)
    from juggle_tmux import reap_stale_agents
    try:
        reap_stale_agents(db, mgr)
    except Exception:
        pass
```

- [ ] **Step 4: Remove reap block from `juggle_cli.py` main**

In `src/juggle_cli.py`, remove lines 904–913:

```python
# Remove:
    # Reap stale agents on every CLI invocation (skip in test mode)
    if "_JUGGLE_TEST_DB" not in os.environ:
        try:
            from juggle_tmux import reap_stale_agents, JuggleTmuxManager

            _reap_db = get_db()
            _reap_mgr = JuggleTmuxManager()
            reap_stale_agents(_reap_db, _reap_mgr)
        except Exception:
            pass  # Non-fatal; reaper can be skipped
```

- [ ] **Step 5: Remove reap call from `juggle_cmd_agents.py`**

In `src/juggle_cmd_agents.py`, remove lines 538–541:

```python
# Remove:
    # Purge stale agents
    from juggle_tmux import reap_stale_agents

    reap_stale_agents(db, mgr)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
uv run pytest tests/test_watchdog_reaper.py tests/test_reaper.py -v
```

Expected: all pass.

- [ ] **Step 7: Run full suite**

```bash
uv run pytest -q
```

Expected: no regressions.

- [ ] **Step 8: Commit**

```bash
git add scripts/juggle-agent-watchdog src/juggle_cli.py src/juggle_cmd_agents.py \
        tests/test_watchdog_reaper.py
git commit -m "feat(watchdog): make watchdog sole periodic reaper

Move reap_stale_agents from CLI main() and cmd_get_agent into the watchdog's
_poll_once loop. Read paths (CLI dispatch, agent assignment) no longer perform
destructive operations as a side effect. The watchdog fires every 30s via
JUGGLE_WATCHDOG_INTERVAL (unchanged). Task D adds dead-watchdog detection
so a silent watchdog failure is surfaced rather than silently skipping reaps."
```

---

## Task D: Dead-Watchdog Heartbeat

**Files:**
- Create: `src/juggle_watchdog_health.py`
- Modify: `scripts/juggle-agent-watchdog` (write heartbeat per poll)
- Modify: `src/juggle_cli.py` (warn + emergency reap if watchdog dead)
- Test: `tests/test_watchdog_health.py` (new)

- [ ] **Step 1: Write failing tests**

Create `tests/test_watchdog_health.py`:

```python
"""Tests for watchdog heartbeat health detection."""
import sys
import time
from pathlib import Path
from unittest import mock
import tempfile
import os

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_is_watchdog_alive_returns_true_when_fresh(tmp_path):
    """Fresh heartbeat file → watchdog is alive."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    hb.write_text(str(time.time()))

    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is True


def test_is_watchdog_alive_returns_false_when_stale(tmp_path):
    """Heartbeat file older than stale_secs → watchdog is dead."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    hb.write_text(str(time.time()))
    # Back-date the mtime by 300 seconds
    old_time = time.time() - 300
    os.utime(hb, (old_time, old_time))

    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is False


def test_is_watchdog_alive_returns_false_when_missing(tmp_path):
    """No heartbeat file → watchdog has never run or was cleaned up."""
    from juggle_watchdog_health import is_watchdog_alive

    hb = tmp_path / "watchdog_heartbeat"
    assert is_watchdog_alive(heartbeat_path=hb, stale_secs=120) is False


def test_write_heartbeat_touches_file(tmp_path):
    """write_heartbeat must create/update the heartbeat file."""
    from juggle_watchdog_health import write_heartbeat

    hb = tmp_path / "watchdog_heartbeat"
    before = time.time()
    write_heartbeat(heartbeat_path=hb)
    after = time.time()

    assert hb.exists()
    mtime = hb.stat().st_mtime
    assert before <= mtime <= after + 1


def test_write_heartbeat_updates_existing_file(tmp_path):
    """write_heartbeat must update mtime even if file already exists."""
    from juggle_watchdog_health import write_heartbeat

    hb = tmp_path / "watchdog_heartbeat"
    old_time = time.time() - 300
    hb.write_text("old")
    os.utime(hb, (old_time, old_time))

    write_heartbeat(heartbeat_path=hb)

    assert hb.stat().st_mtime > old_time
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_watchdog_health.py -v
```

Expected: ImportError or 5 FAIL (module doesn't exist yet).

- [ ] **Step 3: Create `src/juggle_watchdog_health.py`**

```python
"""Watchdog liveness detection via heartbeat file.

The watchdog writes a heartbeat on every _poll_once. CLI commands check
is_watchdog_alive() to decide whether to warn the user about missing reaps.
"""
from __future__ import annotations

import time
from pathlib import Path

HEARTBEAT_PATH = Path.home() / ".juggle" / "watchdog_heartbeat"
_DEFAULT_STALE_SECS = 120  # 4× the default 30s poll interval


def write_heartbeat(heartbeat_path: Path = HEARTBEAT_PATH) -> None:
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.touch()


def is_watchdog_alive(
    heartbeat_path: Path = HEARTBEAT_PATH,
    stale_secs: int = _DEFAULT_STALE_SECS,
) -> bool:
    try:
        mtime = heartbeat_path.stat().st_mtime
        return (time.time() - mtime) < stale_secs
    except FileNotFoundError:
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_watchdog_health.py -v
```

Expected: 5 PASS.

- [ ] **Step 5: Wire `write_heartbeat` into `_poll_once` in watchdog script**

In `scripts/juggle-agent-watchdog`, add import at top (near other `from juggle_*` imports):

```python
from juggle_watchdog_health import write_heartbeat
```

At the start of `_poll_once` (first line of function body, before the snapshot_dir assignment):

```python
def _poll_once(db: JuggleDB, mgr: JuggleTmuxManager) -> None:
    write_heartbeat()
    snapshot_dir, recovery_dir = _get_dirs()
    ...
```

- [ ] **Step 6: Add dead-watchdog warning to `juggle_cli.py`**

In `src/juggle_cli.py`, in `main()`, after `args = parser.parse_args()` and the now-removed reap block, add:

```python
    # Warn if watchdog is dead (it owns periodic reaping as of Task C)
    if "_JUGGLE_TEST_DB" not in os.environ:
        try:
            from juggle_watchdog_health import is_watchdog_alive
            if not is_watchdog_alive():
                import sys as _sys
                print(
                    "Warning: juggle watchdog is not running or unresponsive. "
                    "Start it with: juggle watchdog start",
                    file=_sys.stderr,
                )
        except Exception:
            pass
```

- [ ] **Step 7: Run tests**

```bash
uv run pytest tests/test_watchdog_health.py tests/test_watchdog_reaper.py \
               tests/test_reaper.py tests/test_cockpit_readonly.py -v
```

Expected: all pass.

- [ ] **Step 8: Run full suite**

```bash
uv run pytest -q
```

Expected: no regressions.

- [ ] **Step 9: Commit**

```bash
git add src/juggle_watchdog_health.py scripts/juggle-agent-watchdog \
        src/juggle_cli.py tests/test_watchdog_health.py
git commit -m "feat(watchdog): heartbeat file + dead-watchdog warning in CLI

The watchdog writes ~/.juggle/watchdog_heartbeat on every poll. CLI commands
check is_watchdog_alive() (stale threshold: 120s = 4× poll interval) and
print a stderr warning when the watchdog is not responding. This makes a
silent watchdog failure observable rather than causing panes to accumulate
undetected. Auto-restart is left for a future task — warning first."
```

---

## Devil's Advocate

### Is "watchdog as sole reaper" the right architecture?

**Concern 1: Dead watchdog → unbounded pane accumulation.**
The old approach — every CLI command calling `reap_stale_agents` — was a compensating control for this. Removing it leaves only the watchdog.

*Verdict:* Task D addresses this directly. A dead watchdog is now observable (stderr warning on every CLI call). The right fix for "watchdog might die" is to detect and surface it, not to spread reaping responsibility to every read path. Task D is the load-bearing piece of C.

**Concern 2: Could we close the race at spawn time instead?**
Reordering `spawn_agent` so `db.create_agent` is called before `start_claude_in_pane` would close the race at the source. No grace logic needed.

*Verdict:* Valid alternative for Task A. However, the current `spawn_agent` may rely on having the pane_id available before the DB insert, and reordering requires verifying there are no callers that depend on `pane_id` being set in the DB row by the time the pane starts. Task A's approach (grace check in Pass 2) is safer and minimal. The spawn-reorder can be a follow-up.

**Concern 3: The CLI warning is noisy — will it fire on every command if watchdog is misconfigured?**
Yes. If the user hasn't started the watchdog, every `juggle` command prints a warning. This is intentional — the watchdog being down is an observable problem — but it might frustrate users who are deliberately running without it.

*Verdict:* Acceptable. The warning only fires if `_JUGGLE_TEST_DB` is not set (i.e., not in tests) and the heartbeat file is absent or stale. A settings key (`watchdog.suppress_dead_warning: true`) can be added if this becomes friction. Out of scope for this plan.

**Concern 4: The AST-based tests in Task C are fragile.**
If `cmd_get_agent` is renamed or split, the tests break with `assert func is not None`.

*Verdict:* Acceptable tradeoff. The tests document the architectural constraint and will catch accidental re-introduction of the reap call. They're fast and require no DB/tmux setup. If the function is renamed, updating the test is a one-liner.

**Recommend implementing A+B+C+D as written.** A and B are independently safe to ship. C without D would be risky (silent dead watchdog); D without C would be confusing (warning but CLI still reaps anyway). Land all four on the feature branch before merging.

---

## Open Questions

- [ ] **OQ1**: Should `cmd_get_agent` retain its reap call when the watchdog is dead (as a fallback), or remove it unconditionally and rely on Task D's warning to drive the user to restart the watchdog? This plan removes it unconditionally; if the user wants a fallback, the lazy-reap approach needs design.
- [ ] **OQ2**: Should the CLI warning also attempt a watchdog auto-restart (`juggle watchdog start` inline)? Deferred — warrants its own spec because auto-restart needs PID management and output capture.
- [ ] **OQ3**: The `_get_pane_start_time` helper calls `tmux display-message` with `-t pane_id`. If the pane disappears between `list-panes` and `display-message`, tmux returns an error. The current implementation returns `None` (conservative skip). Should a second call to `list-panes` confirm the pane is truly gone before skipping? Deferred — the conservative skip is correct behavior; the pane would be caught on the next reap cycle.
