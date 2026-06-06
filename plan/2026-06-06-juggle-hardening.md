# Juggle Hardening — Implementation Plan

**Date:** 2026-06-06  
**Scope:** 4 fixes for agent repo context, role templates, worktree finalization, watchdog restart  
**Order:** Sequential (all touch `juggle_cli.py` + `juggle_db.py`; Fix 1→2→3→4)

---

## Fix 1 — Agent Repo Context (repo_path in agents table)

### Root cause

`cmd_get_agent` calls `db.get_ranked_idle_agents(thread_uuid, role)` → picks the first idle agent whose pane is ready. No cwd/repo awareness. A recycled agent from a different repo picks up a task, operates in the wrong directory, and reports false BLOCKERs.

### Implementation

#### 1a. DB migration (doctor)

Add Migration 33 to `juggle_db.py:_migrate()`:

- **Column:** `repo_path TEXT` on `agents` table
- **Migration pattern:** Same as Migration 32 (presence-based via `PRAGMA table_info`)
- **Default:** `NULL` (existing agents get NULL = "unknown repo")

```python
# Migration 33: repo_path on agents
agents_cols = {
    r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
}
if "repo_path" not in agents_cols:
    try:
        conn.execute("ALTER TABLE agents ADD COLUMN repo_path TEXT")
        conn.commit()
        _log.info("Migration 33: repo_path column added to agents")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 33 (repo_path) skipped: %s", e)
```

Also update `CREATE_AGENTS` DDL to include `repo_path TEXT` so fresh DBs get it.

#### 1b. Record repo_path at spawn

`JuggleTmuxManager.spawn_agent()` — after calling `db.create_agent()`, detect cwd:

```python
repo_path = subprocess.check_output(
    ["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"],
    text=True
).strip()
db.update_agent(agent_id, repo_path=repo_path)
```

If `git rev-parse` fails (non-git dir), set `repo_path` to `""` (empty string = not a git repo).

Also update `db.create_agent()` signature to accept optional `repo_path=None` and store it.

#### 1c. get-agent --repo filtering

Add `--repo` flag to `get-agent` subcommand (default: current cwd's git toplevel, or `""` if not a repo). In `cmd_get_agent`:

```python
def _get_repo_for_cwd():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True, cwd=os.getcwd()
        ).strip()
    except subprocess.CalledProcessError:
        return ""

target_repo = getattr(args, "repo", None) or _get_repo_for_cwd()
```

Then, after getting idle candidates from `get_ranked_idle_agents`, filter:

```python
def _repo_matches(agent, target_repo):
    agent_repo = agent.get("repo_path")
    # NULL = pre-migration agent → assume mismatch (treat as incompatible)
    if agent_repo is None:
        return False
    if not target_repo:
        return True  # no repo context requested
    return agent_repo == target_repo
```

**Design decision: skip vs decommission.** Mismatched idle agents are **skipped** (not decommissioned). Rationale:
- Decommissioning kills useful agents that could serve other threads
- A mismatched agent can serve a different thread with a matching repo next time
- Decommission + respawn doubles tmux overhead for no gain
- The idle pool shrinks naturally via `agent_idle_ttl_secs` + `reap_stale_agents`

If no idle agent matches the repo, spawn a fresh one (existing fallback in `cmd_get_agent`).

#### 1d. Agent verification

**Test:** `tests/test_juggle_db_agents.py` — add:

```python
def test_agent_repo_path_stored_on_create(db):
    agent_id = db.create_agent(role="coder", pane_id="%1", repo_path="/home/user/myproject")
    agent = db.get_agent(agent_id)
    assert agent["repo_path"] == "/home/user/myproject"

def test_agent_repo_path_nullable(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    agent = db.get_agent(agent_id)
    assert agent["repo_path"] is None

def test_ranked_idle_agents_includes_repo_path(db):
    db.create_agent(role="coder", pane_id="%1", repo_path="/repo/a")
    agents = db.get_ranked_idle_agents("thread-1")
    assert agents[0].get("repo_path") == "/repo/a"
```

**Agent verification command:**
```bash
# After migration
uv run python -c "from juggle_db import JuggleDB; db=JuggleDB(); db.init_db(); print('agents columns:', [r['name'] for r in db._connect().execute('PRAGMA table_info(agents)').fetchall()])"
# Expected: 'repo_path' in output

# get-agent with repo
juggle get-agent <thread> --repo /path/to/repo
# Expected: returns agent with matching repo_path, or spawns new with correct cwd
```

---

## Fix 2 — Role Task Templates

### Root cause

Every task file repeats 60+ lines of boilerplate (TDD discipline, finalize instructions, quality gate, scope rules). This bloats context and creates drift (planners, coders, researchers get slightly different versions).

### Implementation

#### 2a. Add task_templates to DEFAULTS

In `juggle_settings.py:DEFAULTS`, add:

```python
"task_templates": {
    "coder": (
        "## Role: Coder\n\n"
        "Implement exactly what is specified — no more. Minimal diff.\n\n"
        "### TDD Discipline\n"
        "1. Write failing tests FIRST — confirm they FAIL before implementation\n"
        "2. Implement the minimum code to pass tests\n"
        "3. Run the full test suite — fix any regressions\n"
        "4. Run pre-pr quality gate ({quality_gate_skill}) before completion\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle complete-agent <thread> \"<summary>\" --retain \"<key finding>\"\n"
        "Pre-existing test failures are NOT your concern — document in --retain and proceed.\n\n"
        "### Scope\n"
        "- Only files directly related to the task\n"
        "- No refactoring, cleanup, or bonus work\n"
        "- Do NOT modify AGENTS.md, CLAUDE.md, or .codegraph files\n"
    ),
    "planner": (
        "## Role: Planner\n\n"
        "Produce plans a coder can execute without clarification.\n\n"
        "### Plan Requirements\n"
        "- Every step must be verifiable by an agent (deterministic command + expected output)\n"
        "- Batch unresolved questions in --open-questions; do not ask interactively\n"
        "- Include devil's-advocate section: weakest assumption per fix + failure mode + mitigation\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle complete-agent <thread> \"<summary>\" --open-questions '<json>'\n\n"
        "### Scope\n"
        "- Write the plan file only — never implement\n"
        "- No research beyond what's needed to ground the plan in real code\n"
        "- Open the plan in Obsidian after writing\n"
    ),
    "researcher": (
        "## Role: Researcher\n\n"
        "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.\n\n"
        "### Research Standards\n"
        "- Cite sources with URLs and retrieval dates\n"
        "- Distinguish facts from opinions\n"
        "- Cross-reference at least 2 sources for key claims\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle complete-agent <thread> \"<summary>\" --retain \"<key finding>\"\n\n"
        "### Scope\n"
        "- Research only — no implementation, no code changes\n"
        "- Stay within the research topic; no tangent deep-dives\n"
    ),
}
```

**Key design points:**
- `{quality_gate_skill}` is filled at render time from `settings["agent"]["quality_gate_skill"]`
- Templates are overrideable via `~/.juggle/config.json` → `task_templates` key
- Each template is compact (~15 lines) covering essentials

#### 2b. Template prepend in cmd_send_task

In `cmd_send_task` after reading the prompt file and before prepending `UNIVERSAL_PREAMBLE`:

```python
def _get_task_template(role: str) -> str:
    templates = get_settings().get("task_templates", {})
    template = templates.get(role, "")
    if template:
        qg = get_settings()["agent"].get("quality_gate_skill", "mike:pre-pr")
        template = template.replace("{quality_gate_skill}", qg)
    return template

# In cmd_send_task:
skip_template = getattr(args, "no_template", False)
if not skip_template:
    template = _get_task_template(role)
    prompt = template + "\n---\n\n" + prompt.rstrip() if template else prompt.rstrip()
full_prompt = UNIVERSAL_PREAMBLE + prompt
```

#### 2c. --no-template escape hatch

Add `--no-template` flag to `send-task` subcommand (default `False`):

```python
p_send_task.add_argument(
    "--no-template",
    action="store_true",
    help="Skip role template prepend (use raw prompt file content only)",
)
```

#### 2d. Override via config.json

Users override in `~/.juggle/config.json`:

```json
{
  "task_templates": {
    "coder": "## Custom Coder Rules\n\n...",
    "planner": null
  }
}
```

`null` means "no template for this role" (same as `--no-template`). Missing keys fall back to DEFAULTS. Settings merge uses `_deep_merge`, which handles this correctly — a user key with a string value overrides the DEFAULTS string.

#### 2e. Agent verification

**Test:** `tests/test_juggle_settings.py` — add:

```python
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
```

**Agent verification command:**
```bash
# Verify template exists in DEFAULTS
uv run python -c "from juggle_settings import DEFAULTS; print(list(DEFAULTS['task_templates'].keys()))"
# Expected: ['coder', 'planner', 'researcher']

# Verify --no-template skips prepend (check agent.last_task in DB)
juggle send-task <agent_id> /tmp/test-prompt.txt --no-template
# Agent's last_task should NOT contain template header

# Verify config override
echo '{"task_templates":{"coder":"Custom only"}}' > /tmp/test-config.json
_JUGGLE_CONFIG_PATH=/tmp/test-config.json uv run python -c \
  "from juggle_settings import get_settings; print(get_settings()['task_templates']['coder'])"
# Expected: "Custom only"
```

---

## Fix 3 — Orchestrator-Owned Worktree Finalization

### Root cause

The worktree merge+remove+branch-delete sequence is currently the coder agent's responsibility (in `commands/delegate.md` and `commands/start.md`). Agents either forget or die first, leaving orphaned worktrees. Multiple real incidents reported.

### Design

Move finalization from agent prompt to `complete-agent` CLI command. The orchestrator records worktree metadata at dispatch time; `complete-agent` runs the git operations BEFORE marking the thread closed.

### Implementation

#### 3a. Thread metadata columns (Migration 34)

Add to `threads` table (via `_migrate`):

```sql
ALTER TABLE threads ADD COLUMN worktree_path TEXT;
ALTER TABLE threads ADD COLUMN worktree_branch TEXT;
ALTER TABLE threads ADD COLUMN main_repo_path TEXT;
```

Also update `CREATE_THREADS` DDL.

Update `JuggleDB.update_thread()` to accept optional `worktree_path`, `worktree_branch`, `main_repo_path` keyword args.

#### 3b. Record worktree at dispatch time

Add `--worktree-path`, `--worktree-branch`, `--main-repo-path` flags to `send-task` subcommand. The orchestrator sets these when dispatching into a worktree:

```bash
juggle send-task <agent_id> prompt.txt \
  --worktree-path /tmp/juggle-UB \
  --worktree-branch cyc_UB \
  --main-repo-path /Users/mikechen/github/juggle
```

In `cmd_send_task`, look up the agent's `assigned_thread` and record:

```python
agent = db.get_agent(args.agent_id)
thread_uuid = agent["assigned_thread"]
if thread_uuid:
    db.update_thread(thread_uuid,
        worktree_path=getattr(args, "worktree_path", None),
        worktree_branch=getattr(args, "worktree_branch", None),
        main_repo_path=getattr(args, "main_repo_path", None),
    )
```

#### 3c. Finalization in cmd_complete_agent

At the top of `cmd_complete_agent` (after resolving thread, before closing), add:

```python
def _finalize_worktree(thread: dict) -> tuple[bool, str]:
    """Finalize a worktree: ff-merge → remove → branch-delete.
    Returns (success: bool, message: str). Never destroys unmerged commits.
    """
    worktree_path = (thread.get("worktree_path") or "").strip()
    worktree_branch = (thread.get("worktree_branch") or "").strip()
    main_repo_path = (thread.get("main_repo_path") or "").strip()
    
    if not worktree_path or not worktree_branch or not main_repo_path:
        return True, ""  # No worktree to finalize
    
    if not Path(worktree_path).exists():
        return True, f"Worktree already removed: {worktree_path}"
    
    if not Path(main_repo_path).exists():
        return False, f"Main repo not found: {main_repo_path}"
    
    # 1. Try ff-only merge from worktree branch
    result = subprocess.run(
        ["git", "-C", main_repo_path, "merge", "--ff-only", worktree_branch],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, (
            f"Cannot ff-merge {worktree_branch} into main. "
            f"Worktree left at {worktree_path}. Manual resolution required."
        )
    
    # 2. Remove worktree
    result = subprocess.run(
        ["git", "-C", main_repo_path, "worktree", "remove", worktree_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, f"Worktree remove failed: {result.stderr.strip()}"
    
    # 3. Delete branch
    result = subprocess.run(
        ["git", "-C", main_repo_path, "branch", "-d", worktree_branch],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return True, f"Merged + worktree removed, but branch delete failed: {result.stderr.strip()}"
    
    return True, f"Worktree {worktree_path} finalized (merged {worktree_branch})."
```

Integrated into `cmd_complete_agent`:

```python
# Before closing thread:
ft_success, ft_msg = _finalize_worktree(thread)
if not ft_success:
    db.add_action_item(
        thread_id=thread_uuid,
        message=f"⚠️ Worktree finalization failed: {ft_msg}",
        type_="manual_step",
        priority="high",
    )
    # Append warning to result summary
    args.result_summary = f"{args.result_summary} [WARNING: worktree not finalized — {ft_msg}]"
```

**Failure modes handled:**

| Failure | Behavior |
|---------|----------|
| Non-ff merge (main diverged) | Leave worktree, create HIGH action item, mark complete with warning |
| Dirty worktree (uncommitted changes) | `git worktree remove` fails, leave worktree, warn |
| Worktree already removed | Skip, return success |
| Main repo not found | Warn, skip, create action item |
| Branch already deleted | After successful merge+remove, branch -d fails → log, still success |

**NEVER destroy unmerged commits** — `--ff-only` guarantees this.

#### 3d. Agent verification

**Test:** `tests/test_completion_commands.py` — add:

```python
def test_finalize_worktree_success(tmp_path):
    # Create a mock main repo + worktree
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "-C", str(main), "init"], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"], check=True)
    # Simulate worktree setup...
    # Call _finalize_worktree, verify success

def test_finalize_worktree_non_ff_leaves_worktree(tmp_path):
    # Diverged main → merge fails → returns failure, worktree intact

def test_finalize_worktree_already_removed(tmp_path):
    # No worktree directory → success (True, "already removed")

def test_finalize_worktree_no_metadata_skips():
    # Thread has no worktree_path → success, no-op
```

**Agent verification command:**
```bash
# Create test scenario
mkdir -p /tmp/test-repo && cd /tmp/test-repo
git init && git commit --allow-empty -m "init"
git worktree add /tmp/test-wt -b test-branch HEAD
cd /tmp/test-wt && touch newfile && git add . && git commit -m "test"

# Simulate complete-agent finalization (via code path)
uv run juggle complete-agent <thread> "done"
# Verify: /tmp/test-wt removed, test-branch deleted, commits merged to main
ls /tmp/test-wt 2>&1  # should fail (not found)
git -C /tmp/test-repo branch  # test-branch should not appear
```

---

## Fix 4 — juggle start Always Restarts Watchdog

### Root cause

`cmd_start` already calls `_start_watchdog()` which kills via pidfile then starts fresh. However, stale watchdog processes from crashed sessions (where the pidfile was lost) aren't caught. A global `pkill` sweep ensures no zombie accumulation.

### Implementation

#### 4a. Add pkill before session-scoped kill

In `_start_watchdog()` (in `juggle_cmd_threads.py`, currently line 50-105), add a global `pkill` step BEFORE the session-scoped pidfile kill:

```python
def _start_watchdog() -> None:
    import subprocess
    import time
    
    # Step 0: Global sweep — kill any stale watchdog processes
    # regardless of session (pidfiles may be lost).
    try:
        subprocess.run(
            ["pkill", "-f", "juggle-agent-watchdog"],
            capture_output=True,
            timeout=5,
        )
        time.sleep(0.5)  # Let processes exit cleanly
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # pkill not available or timed out — continue
    
    pid_file = _watchdog_pid_file()
    # ... existing pidfile-based kill logic (unchanged) ...
```

**Rationale:** The existing pidfile-based kill handles the normal case (same session). The `pkill` sweep handles:
- Orphaned process from a crashed Claude Code session
- Manual `juggle-agent-watchdog` invocation without pidfile
- Race condition where pidfile was deleted but process still runs

#### 4b. Idempotency guarantee

After this fix, `juggle start` is fully idempotent:
- Call 1: pkill (no-op if nothing running) → kill by pidfile (no-op if first call) → start fresh
- Call 2: pkill kills the one from call 1 → kill by pidfile → start fresh
- Crash recovery: pkill catches orphan → start fresh

No zombie accumulation.

#### 4c. Agent verification

**Test:** `tests/test_cmd_threads.py` — add:

```python
def test_start_watchdog_pkill_called(monkeypatch):
    import subprocess
    pkill_calls = []
    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "pkill" in str(cmd):
            pkill_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "kill", lambda *a: None)
    # Mock pidfile exists with dead pid
    from juggle_cmd_threads import _start_watchdog
    _start_watchdog()
    assert len(pkill_calls) >= 1

def test_start_watchdog_idempotent(monkeypatch, tmp_path):
    # Mock pidfile path to tmp_path
    # Call twice, verify no exception
    _start_watchdog()
    _start_watchdog()  # should not raise
```

**Agent verification command:**
```bash
# Start watchdog
juggle start
# Verify process exists
pgrep -f juggle-agent-watchdog

# Start again (should kill old, start new)
juggle start
# Verify exactly ONE process exists
test "$(pgrep -f juggle-agent-watchdog | wc -l)" -eq 1

# Kill manually (simulate crash losing pidfile)
pkill -9 -f juggle-agent-watchdog
rm -f ~/.juggle/watchdog*.pid
# Start again
juggle start
# Verify watchdog is running
pgrep -f juggle-agent-watchdog
```

---

## Implementation Order & Dependency Graph

```
Fix 1 (repo_path)  ──┐
                      ├──>  Fix 3 (worktree)  ──>  Fix 2 (templates)  ──>  Fix 4 (watchdog)
                      │
```

**Rationale:** Fix 1 adds `repo_path` to agents table. Fix 3 adds worktree columns to threads + modifies `complete-agent`. Fix 2 modifies `send-task` which Fix 3 also extends (worktree flags). Fix 4 is independent but simplest — placed last.

**Each fix includes:**
1. Failing test(s) written FIRST (TDD)
2. Implementation (DB migration + CLI logic + settings)
3. Tests pass
4. Entry in CHANGELOG.md

---

## Migration Safety

- **Doctor (`juggle doctor`):** Runs `init_db()` → `_migrate()`. All migrations use presence-based checks (`PRAGMA table_info`, `SELECT name FROM sqlite_master`), fully idempotent.
- **New columns:** `repo_path TEXT`, `worktree_path TEXT`, `worktree_branch TEXT`, `main_repo_path TEXT` — all nullable, no data loss.
- **Backward compat:** NULL columns behave correctly: agents with NULL `repo_path` are treated as incompatible for `--repo` filtering; threads with NULL `worktree_path` skip finalization.
- **Rollback:** Standard SQLite `ALTER TABLE ... ADD COLUMN` is not reversible, but NULL columns are harmless. To revert, drop and recreate from pre-migration backup.

---

## Devil's Advocate

### Fix 1 weakest assumption
**Assumption:** Two repos never share the same absolute path.
**Failure mode:** Container/symlinked repos where the orchestrator's path differs from the agent's.
**Mitigation:** Use `git rev-parse --show-toplevel` (realpath-equivalent). Accept this is good enough for single-machine usage.

### Fix 2 weakest assumption
**Assumption:** Templates are short enough that prepending won't push critical task content past the LLM's attention horizon.
**Failure mode:** Long template + long task → task content de-prioritized.
**Mitigation:** Templates kept at ~15 lines max. `--no-template` escape hatch for token-critical dispatches.

### Fix 3 weakest assumption
**Assumption:** Agent calls `complete-agent` only after committing + pushing, so worktree is merge-ready.
**Failure mode:** Agent commits but worktree is dirty (unstaged changes). `git worktree remove` fails, worktree left intact — safe.
**Mitigation:** `--ff-only` merge never rewrites history. Dirty worktree → remove fails → action item created. No data lost.

### Fix 4 weakest assumption
**Assumption:** `pkill -f juggle-agent-watchdog` matches only Juggle watchdog processes.
**Failure mode:** Unrelated process with matching command line.
**Mitigation:** Extremely unlikely collision. `pkill` fails gracefully (no matches → exit 1, caught by try/except).

---

## Test Summary

| Fix | Test File | New Tests |
|-----|-----------|-----------|
| Fix 1 | `tests/test_juggle_db_agents.py` | 3 (repo_path create, nullable, ranked includes) |
| Fix 1 | `tests/test_juggle_cli.py` | 1 (get-agent --repo integration) |
| Fix 2 | `tests/test_juggle_settings.py` | 2 (template defaults, override) |
| Fix 2 | `tests/test_juggle_cli.py` | 1 (send-task template prepend) |
| Fix 3 | `tests/test_completion_commands.py` | 4 (success, non-ff, already-removed, no-metadata) |
| Fix 4 | `tests/test_cmd_threads.py` | 2 (pkill called, idempotent) |

**Total: 13 new tests across 4 test files.**

Run all: `cd /Users/mikechen/github/juggle && uv run pytest -x tests/`

---

## Files Modified

| File | Changes |
|------|---------|
| `src/juggle_db.py` | Migration 33 (repo_path), Migration 34 (worktree columns), update CREATE_AGENTS + CREATE_THREADS DDL, `create_agent()` signature, `update_thread()` keyword support |
| `src/juggle_settings.py` | Add `task_templates` to DEFAULTS |
| `src/juggle_cli.py` | Add `--repo` to get-agent, `--no-template` + worktree flags to send-task |
| `src/juggle_cmd_agents.py` | `cmd_get_agent`: repo filtering. `cmd_send_task`: template prepend + worktree recording. `cmd_complete_agent`: worktree finalization |
| `src/juggle_cmd_threads.py` | `_start_watchdog`: add `pkill` sweep |
| `src/juggle_tmux.py` | `spawn_agent`: detect and record repo_path |
| `tests/test_juggle_db_agents.py` | Fix 1 DB tests |
| `tests/test_juggle_cli.py` | Fix 1 + Fix 2 CLI tests |
| `tests/test_juggle_settings.py` | Fix 2 settings tests |
| `tests/test_completion_commands.py` | Fix 3 worktree finalization tests |
| `tests/test_cmd_threads.py` | Fix 4 watchdog tests |
| `CHANGELOG.md` | Add 4 hardening entries |
