# Project Assignment Automation — Spec & Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make project auto-assignment accurate, self-improving, and streamlined through a closed-loop feedback system: synthesized `match_profile` fields, drift-triggered silent re-synthesis, INBOX fallback on low confidence, and bulk-assign fixes.

**Architecture:** The classifier (`infer_project_id`) reads a cached `match_profile` alongside `objective` to make better routing decisions; a new `project synth` command regenerates profiles from actual thread contents; a cosine-distance drift detector silently triggers re-synthesis when a project's thread centroid diverges from its profile; after any profile change a bounded INBOX re-sweep re-routes stale threads. A shared `llm_call(prompt, profile=)` dispatcher replaces the single `_cheap_llm_call` and routes to two OpenRouter+fallback tiers.

**Tech Stack:** Python 3.11+, SQLite (existing juggle.db), `urllib.request` (no new HTTP libs), `math`/`statistics` stdlib (cosine similarity — no numpy), existing `juggle_db.JuggleDB`, `juggle_settings.get_settings`, `juggle_cli_common._cheap_llm_call` (to be superseded).

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/juggle_cli_common.py` | **Modify** | Replace `_cheap_llm_call` with `llm_call(prompt, profile="cheap", timeout=10)` dispatcher; keep `_cheap_llm_call` as shim |
| `src/juggle_cmd_projects.py` | **Modify** | Classifier reads `match_profile`; confidence output; bulk/archived assign; `cmd_project_synth`; drift detector; re-sweep |
| `src/juggle_db.py` | **Modify** | Migration 30: `match_profile` + `profile_synth_at` + `profile_dirty` on `projects`; new DB methods |
| `src/juggle_cli.py` | **Modify** | Register `project synth` subcommand |
| `src/juggle_settings.py` | **Modify** | Add `llm_profiles` section with `cheap`/`normal` model ids |
| `tests/test_llm_dispatch.py` | **Create** | Unit tests for `llm_call` profile dispatch |
| `tests/test_project_synth.py` | **Create** | Unit tests for `build_match_profile_prompt`, `drift_score`, synth dirty-tracking, re-sweep |
| `tests/test_projects.py` | **Modify** | Add tests: confidence threshold → INBOX, bulk assign, archived thread assign |
| `skills/project:synthesis.md` | **Create** | Slash command skill stub that calls `project synth` |

---

## Phase 0 — LLM Profile Dispatcher

**What:** Replace `_cheap_llm_call` with a profile-based `llm_call(prompt, profile, timeout)` that reads two named profiles from `juggle_settings`. Keep `_cheap_llm_call` as a one-line shim so all existing callers require zero changes.

**Acceptance:** `llm_call("hi", profile="cheap")` uses the cheap model; `llm_call("hi", profile="normal")` uses the normal model; OpenRouter failure falls back to Claude subprocess; unknown profile raises `ValueError`.

---

### Task 0.1 — Add `llm_profiles` to settings defaults

**Files:**
- Modify: `src/juggle_settings.py` (DEFAULTS dict, around line 302)

- [ ] **Step 1: Add the `llm_profiles` section to DEFAULTS**

In `juggle_settings.py`, inside the `DEFAULTS` dict, add after the `"title_gen"` section:

```python
    # LLM dispatcher profiles (model ids are editable in config.json)
    "llm_profiles": {
        "cheap": {
            "openrouter_model": "deepseek/deepseek-chat-v3-0324:free",
            "fallback_model": "claude-haiku-4-5-20251001",
        },
        "normal": {
            "openrouter_model": "moonshotai/kimi-k2:free",
            "fallback_model": "claude-sonnet-4-6",
        },
    },
```

> **Note on model IDs:** `deepseek/deepseek-chat-v3-0324:free` is the current OpenRouter slug for DeepSeek V3 (cheap tier); `moonshotai/kimi-k2:free` is the Kimi K2 free tier. Both are editable in `~/.juggle/config.json` under `llm_profiles` — no code change needed to swap models. Verify current slugs at openrouter.ai/models before deploying.

- [ ] **Step 2: Run existing settings tests to confirm no regression**

```bash
uv run pytest tests/test_settings.py -q 2>/dev/null || echo "no settings tests — ok"
```

- [ ] **Step 3: Commit**

```bash
git add src/juggle_settings.py
git commit -m "feat(llm): add llm_profiles section to settings defaults"
```

---

### Task 0.2 — Implement `llm_call` dispatcher and shim

**Files:**
- Modify: `src/juggle_cli_common.py`
- Create: `tests/test_llm_dispatch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_llm_dispatch.py`:

```python
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _mock_urlopen(response_text: str):
    """Return a context-manager mock that simulates urllib urlopen."""
    import json as _json
    resp = MagicMock()
    resp.read.return_value = _json.dumps({
        "choices": [{"message": {"content": response_text}}]
    }).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_llm_call_cheap_uses_cheap_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="cheap")
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert "deepseek" in parsed["model"]


def test_llm_call_normal_uses_normal_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", return_value=_mock_urlopen("result")):
        with patch("urllib.request.Request") as mock_req:
            llm_call("hello", profile="normal")
    body = mock_req.call_args[0][1]
    import json
    parsed = json.loads(body)
    assert "kimi" in parsed["model"]


def test_llm_call_unknown_profile_raises():
    from juggle_cli_common import llm_call
    with pytest.raises(ValueError, match="Unknown LLM profile"):
        llm_call("hello", profile="bogus")


def test_llm_call_openrouter_failure_falls_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_KEY", "testkey")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import llm_call
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(returncode=0, stdout="fallback result")
            result = llm_call("hello", profile="cheap")
    assert result == "fallback result"


def test_cheap_llm_call_shim_still_works(tmp_path, monkeypatch):
    """_cheap_llm_call must remain callable and delegate to llm_call."""
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(tmp_path / "config.json"))
    from juggle_cli_common import _cheap_llm_call
    with patch("juggle_cli_common.llm_call", return_value="ok") as mock_llm:
        result = _cheap_llm_call("test")
    mock_llm.assert_called_once_with("test", profile="cheap", timeout=10)
    assert result == "ok"
```

- [ ] **Step 2: Run tests — confirm they fail**

```bash
uv run pytest tests/test_llm_dispatch.py -q
```
Expected: `ImportError` or `AttributeError: module 'juggle_cli_common' has no attribute 'llm_call'`

- [ ] **Step 3: Implement `llm_call` in `juggle_cli_common.py`**

Add after the `get_db` function (around line 43) and replace `_cheap_llm_call` (lines 152–190):

```python
def llm_call(prompt: str, profile: str = "cheap", timeout: int = 10) -> str | None:
    """Profile-based LLM dispatcher.

    Profiles defined in settings.llm_profiles (cheap / normal).
    Flow: OpenRouter primary -> Claude subprocess fallback -> None.
    """
    import json as _json
    from juggle_settings import get_settings
    profiles = get_settings().get("llm_profiles", {})
    if profile not in profiles:
        raise ValueError(f"Unknown LLM profile: {profile!r}. Valid: {list(profiles)}")
    cfg = profiles[profile]
    api_key = os.environ.get("OPENROUTER_KEY", "")
    if api_key:
        try:
            body = _json.dumps({
                "model": cfg["openrouter_model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
            }).encode()
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.loads(resp.read())
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                logging.info("llm_call(%s): openrouter -> %r", profile, text[:60])
                return text
        except Exception as e:
            logging.warning("llm_call(%s): openrouter failed: %s", profile, e)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", cfg["fallback_model"]],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            logging.info("llm_call(%s): fallback -> %r", profile, result.stdout.strip()[:60])
            return result.stdout.strip()
    except Exception as e:
        logging.warning("llm_call(%s): fallback failed: %s", profile, e)
    return None


def _cheap_llm_call(prompt: str, timeout: int = 10) -> str | None:
    """Shim: delegates to llm_call(profile='cheap'). Kept for call-site compat."""
    return llm_call(prompt, profile="cheap", timeout=timeout)
```

Also add `import urllib.request` at the top if not already present (it is already imported inside the old `_cheap_llm_call` body — move it to module level).

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_llm_dispatch.py -q
```
Expected: 5 passed

- [ ] **Step 5: Run full test suite to check no regressions**

```bash
uv run pytest tests/ -q --ignore=tests/test_juggle_hooks.py -x
```

- [ ] **Step 6: Commit**

```bash
git add src/juggle_cli_common.py tests/test_llm_dispatch.py
git commit -m "feat(llm): profile-based llm_call dispatcher; _cheap_llm_call shim"
```

---

## Phase 1 — `match_profile` Schema + Classifier Integration

**What:** Add `match_profile`, `profile_synth_at`, `profile_dirty` columns to the `projects` table (Migration 30). Update the classifier prompt to include `match_profile` when present. The hot path is read-only (no LLM in this phase).

**Acceptance:** New columns exist after `init_db()`; `_build_classifier_prompt` includes match_profile text when non-empty; classifier behavior unchanged when match_profile is empty (backward compat).

---

### Task 1.1 — DB Migration 30: `match_profile`, `profile_synth_at`, `profile_dirty`

**Files:**
- Modify: `src/juggle_db.py`
- Modify: `tests/test_projects_db.py`

- [ ] **Step 1: Add DDL constants and migration**

In `juggle_db.py`, the `CREATE_PROJECTS` constant (line ~187) stays unchanged. Add Migration 30 at the end of `_migrate()` (after Migration 29, ~line 729):

```python
        # Migration 30: match_profile + profile_synth_at + profile_dirty on projects
        proj_cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
        try:
            if "match_profile" not in proj_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN match_profile TEXT DEFAULT ''")
            if "profile_synth_at" not in proj_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN profile_synth_at TEXT")
            if "profile_dirty" not in proj_cols:
                conn.execute("ALTER TABLE projects ADD COLUMN profile_dirty INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            _log.info("Migration 30: match_profile columns added to projects")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 30 (match_profile) skipped: %s", e)
```

Also add these columns to `CREATE_PROJECTS` so fresh DBs get them from the start:

```python
CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  objective        TEXT NOT NULL DEFAULT '',
  success_criteria TEXT NOT NULL DEFAULT '[]',
  out_of_scope     TEXT DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',
  summary          TEXT DEFAULT '',
  closed_at        TEXT,
  created_at       TEXT NOT NULL,
  last_active      TEXT NOT NULL,
  match_profile    TEXT DEFAULT '',
  profile_synth_at TEXT,
  profile_dirty    INTEGER NOT NULL DEFAULT 0
);
"""
```

- [ ] **Step 2: Add DB helper methods**

In `JuggleDB`, after `update_project` (line ~1034), add:

```python
def set_match_profile(self, project_id: str, match_profile: str) -> None:
    now = _now()
    with self._connect() as conn:
        conn.execute(
            "UPDATE projects SET match_profile=?, profile_synth_at=?, profile_dirty=0 WHERE id=?",
            (match_profile, now, project_id),
        )
        conn.commit()

def mark_project_dirty(self, project_id: str) -> None:
    with self._connect() as conn:
        conn.execute(
            "UPDATE projects SET profile_dirty=1 WHERE id=?",
            (project_id,),
        )
        conn.commit()

def get_dirty_projects(self) -> list[dict]:
    with self._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE profile_dirty=1 AND status NOT IN ('archived','closed') AND id != 'INBOX'",
        ).fetchall()
    return [dict(r) for r in rows]
```

Also update `update_project` allowed fields set to include `match_profile`, `profile_synth_at`, `profile_dirty`:

```python
allowed = {"name", "objective", "success_criteria", "out_of_scope", "status",
           "last_active", "match_profile", "profile_synth_at", "profile_dirty"}
```

- [ ] **Step 3: Write failing tests**

Add to `tests/test_projects_db.py`:

```python
def test_migration_30_adds_match_profile_columns(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    p = db.get_project("INBOX")
    assert "match_profile" in p
    assert "profile_dirty" in p
    assert "profile_synth_at" in p


def test_set_match_profile_clears_dirty(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Test", "Test objective")
    db.mark_project_dirty(pid)
    assert db.get_project(pid)["profile_dirty"] == 1
    db.set_match_profile(pid, "Software dev threads: feature work, CI fixes.")
    p = db.get_project(pid)
    assert p["profile_dirty"] == 0
    assert p["match_profile"] == "Software dev threads: feature work, CI fixes."
    assert p["profile_synth_at"] is not None


def test_get_dirty_projects_excludes_clean(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    p1 = db.create_project("Dirty", "obj")
    p2 = db.create_project("Clean", "obj")
    db.mark_project_dirty(p1)
    dirty = db.get_dirty_projects()
    ids = [p["id"] for p in dirty]
    assert p1 in ids
    assert p2 not in ids
```

- [ ] **Step 4: Run tests — confirm they fail**

```bash
uv run pytest tests/test_projects_db.py::test_migration_30_adds_match_profile_columns tests/test_projects_db.py::test_set_match_profile_clears_dirty tests/test_projects_db.py::test_get_dirty_projects_excludes_clean -q
```

- [ ] **Step 5: Run tests — confirm they pass**

```bash
uv run pytest tests/test_projects_db.py -q
```
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/juggle_db.py tests/test_projects_db.py
git commit -m "feat(projects): migration 30 — match_profile, profile_dirty, profile_synth_at columns"
```

---

### Task 1.2 — Classifier reads `match_profile`

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_projects.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_projects.py`:

```python
def test_build_classifier_prompt_includes_match_profile():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [
        {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI platform",
         "match_profile": "Codebase work: agent dispatch, Terraform, CI. NOT: finance."},
    ]
    prompt = _build_classifier_prompt("fix terraform deploy", projects, {}, [])
    assert "Codebase work" in prompt


def test_build_classifier_prompt_no_match_profile_unchanged():
    from juggle_cmd_projects import _build_classifier_prompt
    projects = [
        {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI platform"},
    ]
    prompt = _build_classifier_prompt("fix terraform deploy", projects, {}, [])
    assert "P1" in prompt
    assert "LifeOS Dev" in prompt
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_projects.py::test_build_classifier_prompt_includes_match_profile tests/test_projects.py::test_build_classifier_prompt_no_match_profile_unchanged -q
```

- [ ] **Step 3: Update `_build_classifier_prompt` to include `match_profile`**

In `juggle_cmd_projects.py`, replace the `_build_classifier_prompt` function (lines 92–121):

```python
def _build_classifier_prompt(
    topic: str,
    projects: list[dict],
    positives_by_project: dict[str, list[dict]],
    corrections: list[dict],
) -> str:
    project_parts = []
    for p in projects:
        part = f'{p["id"]}: {p["name"]} — {p["objective"]}'
        mp = (p.get("match_profile") or "").strip()
        if mp:
            part += f' | profile: {mp}'
        examples = [t["topic"] for t in positives_by_project.get(p["id"], []) if t.get("topic")]
        if examples:
            part += f' | confirmed: {"; ".join(examples)}'
        project_parts.append(part)

    prompt = f'Topic: "{topic}". Projects: [{"; ".join(project_parts)}]. '
    if corrections:
        correction_parts = [
            f'"{c["topic"]}" -> {c["to_project"]}' for c in corrections
        ]
        prompt += f'Past corrections: [{"; ".join(correction_parts)}]. '
    prompt += (
        'Which project fits best? '
        'Return ONLY valid JSON: {"project_id": "<id_or_INBOX>", "confidence": <0.0-1.0>}'
    )
    return prompt
```

> Note: the prompt now requests a `confidence` field. Phase 4 uses it; in this phase `infer_project_id` ignores it (backward compat).

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_projects.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_projects.py
git commit -m "feat(classifier): include match_profile in classification prompt"
```

---

## Phase 2 — Synth Command + Dirty-Tracking

**What:** `project synth [--all | --dirty | <project_id>]` synthesizes `match_profile` for one or more projects. Pure helper function `build_match_profile_prompt(project, threads, corrections)` is testable with LLM mocked. Mark project dirty when threads are reassigned.

**Acceptance:** `project synth P1` calls the cheap LLM, writes `match_profile`, clears `profile_dirty`; `project synth --dirty` skips clean projects; assigning a thread marks its old project dirty.

---

### Task 2.1 — `build_match_profile_prompt` pure function

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Create: `tests/test_project_synth.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_project_synth.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_build_match_profile_prompt_contains_project_name():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "LifeOS Dev", "objective": "Build AI assistant platform"}
    threads = [
        {"topic": "fix agent dispatch bug", "assigned_by": "human"},
        {"topic": "add terraform module", "assigned_by": "human"},
        {"topic": "auto-assigned CI thread", "assigned_by": "auto"},
    ]
    corrections = [{"topic": "investing script", "from_project": "P1", "to_project": "P2"}]
    prompt = build_match_profile_prompt(project, threads, corrections)
    assert "LifeOS Dev" in prompt
    assert "fix agent dispatch bug" in prompt
    assert "auto-assigned CI thread" in prompt


def test_build_match_profile_prompt_human_weighted_before_auto():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [
        {"topic": "human thread", "assigned_by": "human"},
        {"topic": "auto thread", "assigned_by": "auto"},
    ]
    prompt = build_match_profile_prompt(project, threads, [])
    # Human-assigned appears in "confirmed" section
    assert "human thread" in prompt


def test_build_match_profile_prompt_includes_negative_framing():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    prompt = build_match_profile_prompt(project, [], [])
    # Must ask for negative keywords
    assert "NOT" in prompt or "negative" in prompt.lower() or "sibling" in prompt.lower()


def test_build_match_profile_prompt_bounded_thread_count():
    from juggle_cmd_projects import build_match_profile_prompt
    project = {"id": "P1", "name": "Dev", "objective": "obj"}
    threads = [{"topic": f"thread {i}", "assigned_by": "human"} for i in range(50)]
    prompt = build_match_profile_prompt(project, threads, [])
    # Prompt must not include all 50 thread titles verbatim (bounded at ~30)
    assert prompt.count("thread ") <= 32
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_project_synth.py -q
```
Expected: `ImportError: cannot import name 'build_match_profile_prompt'`

- [ ] **Step 3: Implement `build_match_profile_prompt` in `juggle_cmd_projects.py`**

Add after `_build_classifier_prompt` (around line 123):

```python
_SYNTH_MAX_HUMAN = 20
_SYNTH_MAX_AUTO = 10


def build_match_profile_prompt(
    project: dict,
    threads: list[dict],
    corrections: list[dict],
) -> str:
    """Pure function: build synthesis prompt for one project's match_profile.

    Weights human-assigned threads highest; auto-assigned weakly included to
    avoid feedback-loop reinforcement. Bounded to prevent token overrun.
    """
    human_topics = [t["topic"] for t in threads if t.get("assigned_by") == "human"]
    auto_topics = [t["topic"] for t in threads if t.get("assigned_by") != "human"]
    # Most-recent first (caller should pre-sort by last_active DESC)
    human_sample = human_topics[:_SYNTH_MAX_HUMAN]
    auto_sample = auto_topics[:_SYNTH_MAX_AUTO]

    correction_lines = [
        f'  - "{c["topic"]}" was moved OUT (to {c["to_project"]})'
        for c in (corrections or [])[:5]
    ]

    confirmed_section = "\n".join(f"  - {t}" for t in human_sample) or "  (none yet)"
    auto_section = "\n".join(f"  - {t}" for t in auto_sample) or "  (none yet)"
    correction_section = "\n".join(correction_lines) or "  (none)"

    return (
        f"Synthesize a match_profile for the project below.\n\n"
        f"Project: {project['name']} (id={project['id']})\n"
        f"Objective: {project['objective']}\n\n"
        f"Human-confirmed thread topics (trust these most):\n{confirmed_section}\n\n"
        f"Auto-assigned thread topics (use lightly):\n{auto_section}\n\n"
        f"Topics recently moved OUT of this project:\n{correction_section}\n\n"
        f"Write a match_profile with exactly three lines:\n"
        f"1. A compact 1-2 sentence description of what belongs in this project.\n"
        f"2. KEYWORDS: <5-10 comma-separated signal words>\n"
        f"3. NOT: <5-10 comma-separated words for sibling projects that should NOT match>\n\n"
        f"Output only those three lines. No preamble."
    )
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_project_synth.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_project_synth.py
git commit -m "feat(synth): build_match_profile_prompt pure function + tests"
```

---

### Task 2.2 — `synth_project` function + dirty-tracking on assign

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_project_synth.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_project_synth.py`:

```python
from unittest.mock import patch, MagicMock


def test_synth_project_writes_match_profile(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build things")
    with patch("juggle_cmd_projects.llm_call", return_value=(
        "Software development threads.\nKEYWORDS: code, deploy, CI\nNOT: finance, investing"
    )):
        synth_project(db, pid)
    p = db.get_project(pid)
    assert "Software development" in p["match_profile"]
    assert p["profile_dirty"] == 0
    assert p["profile_synth_at"] is not None


def test_synth_project_skips_if_no_threads_and_no_force(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import synth_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Empty", "No threads yet")
    with patch("juggle_cmd_projects.llm_call") as mock_llm:
        synth_project(db, pid)  # should not call LLM for empty project
    mock_llm.assert_not_called()


def test_assign_marks_old_project_dirty(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import _assign_thread_to_project
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    p1 = db.create_project("P1", "obj1")
    p2 = db.create_project("P2", "obj2")
    tid = db.create_thread("some topic", session_id="s1")
    db.update_thread(tid, project_id=p1, assigned_by="human")
    _assign_thread_to_project(db, tid, p2, assigned_by="human")
    assert db.get_project(p1)["profile_dirty"] == 1
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_project_synth.py::test_synth_project_writes_match_profile tests/test_project_synth.py::test_synth_project_skips_if_no_threads_and_no_force tests/test_project_synth.py::test_assign_marks_old_project_dirty -q
```

- [ ] **Step 3: Implement `synth_project` and `_assign_thread_to_project`**

Add to `juggle_cmd_projects.py` after `build_match_profile_prompt`:

```python
def _assign_thread_to_project(
    db, thread_uuid: str, project_id: str, assigned_by: str = "human"
) -> None:
    """Assign a thread and mark the old project dirty if the project changed."""
    t = db.get_thread(thread_uuid)
    if not t:
        return
    old_project = t.get("project_id", INBOX_PROJECT_ID)
    db.update_thread(thread_uuid, project_id=project_id, assigned_by=assigned_by)
    if old_project != project_id and old_project != INBOX_PROJECT_ID:
        db.mark_project_dirty(old_project)


def synth_project(db, project_id: str, force: bool = False) -> str | None:
    """Synthesize match_profile for one project. Returns new profile or None if skipped.

    Skips if no threads exist and force=False (nothing to learn from).
    """
    from juggle_cli_common import llm_call
    project = db.get_project(project_id)
    if not project:
        return None
    # Gather threads: human-assigned first, then auto, sorted by recency
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT topic, assigned_by FROM threads "
            "WHERE project_id=? AND show_in_list=1 "
            "ORDER BY CASE assigned_by WHEN 'human' THEN 0 ELSE 1 END, last_active DESC",
            (project_id,),
        ).fetchall()
    threads = [dict(r) for r in rows]
    if not threads and not force:
        log.info("synth_project: skipping %s — no threads", project_id)
        return None
    corrections = db.get_recent_corrections(limit=10)
    prompt = build_match_profile_prompt(project, threads, corrections)
    result = llm_call(prompt, profile="cheap", timeout=20)
    if not result:
        log.warning("synth_project: LLM returned None for %s", project_id)
        return None
    db.set_match_profile(project_id, result.strip())
    log.info("synth_project: synthesized profile for %s", project_id)
    return result.strip()
```

Update `cmd_project_assign` to use `_assign_thread_to_project` and support archived threads and bulk IDs (see Task 5.1 for full rewrite — for now just swap internal call):

```python
def cmd_project_assign(args):
    db = get_db(init=True)
    # Support multiple thread_ids
    thread_ids = args.thread_id if isinstance(args.thread_id, list) else [args.thread_id]
    p = db.get_project(args.project_id)
    if not p:
        print(f"Project not found: {args.project_id}")
        sys.exit(1)
    for tid_input in thread_ids:
        t = db.get_thread_by_user_label(tid_input)
        if not t:
            # Also check archived threads
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM threads WHERE user_label=? OR id=?",
                    (tid_input.upper(), tid_input),
                ).fetchone()
            t = dict(row) if row else None
        if not t:
            print(f"Thread not found: {tid_input}")
            continue
        from_project = t.get("project_id", "INBOX")
        _assign_thread_to_project(db, t["id"], args.project_id, assigned_by="human")
        if from_project != args.project_id:
            db.log_project_correction(t["topic"], from_project=from_project, to_project=args.project_id)
        print(f"Thread [{tid_input}] -> project {args.project_id} ({p['name']})")
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_project_synth.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_project_synth.py
git commit -m "feat(synth): synth_project function; assign marks old project dirty"
```

---

### Task 2.3 — `cmd_project_synth` CLI command + skill stub

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `src/juggle_cli.py`
- Create: `skills/project:synthesis.md`

- [ ] **Step 1: Implement `cmd_project_synth`**

Add to `juggle_cmd_projects.py`:

```python
def cmd_project_synth(args):
    db = get_db(init=True)
    if getattr(args, "all", False):
        projects = db.get_active_projects()
    elif getattr(args, "dirty", False):
        projects = db.get_dirty_projects()
    else:
        p = db.get_project(args.project_id)
        if not p:
            print(f"Project not found: {args.project_id}")
            sys.exit(1)
        projects = [p]

    if not projects:
        print("No projects to synthesize.")
        return

    for p in projects:
        pid = p["id"]
        print(f"Synthesizing {pid} ({p['name']})...", end=" ", flush=True)
        result = synth_project(db, pid, force=getattr(args, "all", False))
        if result:
            preview = result.split("\n")[0][:80]
            print(f"done. Profile: {preview}")
        else:
            print("skipped (no threads).")
```

- [ ] **Step 2: Register subcommand in `juggle_cli.py`**

In `juggle_cli.py`, add after the `open` subcommand registration (around line 893), before `args = parser.parse_args()`:

```python
    _p = _ps.add_parser("synth", help="Synthesize match_profile for project(s)")
    _synth_group = _p.add_mutually_exclusive_group()
    _synth_group.add_argument("--all", action="store_true", help="Re-synth all active projects")
    _synth_group.add_argument("--dirty", action="store_true", help="Re-synth only dirty projects")
    _p.add_argument("project_id", nargs="?", help="Project id (omit if --all or --dirty)")
    _p.set_defaults(func=cmd_project_synth)
```

Also add to the import block at the top of `juggle_cli.py`:

```python
    cmd_project_synth,
```

- [ ] **Step 3: Create the skill stub**

Create `skills/project:synthesis.md`:

```markdown
---
name: juggle:project:synthesis
description: Re-synthesize match_profile for one or more projects via `project synth`.
---

Run `project synth` to refresh project match profiles.

Examples:
- `/juggle:project:synthesis` — synth all dirty projects
- `project synth P1` — synth a specific project
- `project synth --all` — force-synth all active projects

The CLI equivalent: `uv run src/juggle_cli.py project synth [--all|--dirty|<id>]`
```

- [ ] **Step 4: Run smoke test**

```bash
export _JUGGLE_TEST_DB="$(mktemp /tmp/juggle_test_XXXXXX.db)"
export CLAUDE_PLUGIN_DATA=/tmp
export JUGGLE_MAX_BACKGROUND_AGENTS=5 JUGGLE_MAX_THREADS=10
uv run src/juggle_cli.py db init
uv run src/juggle_cli.py project synth --dirty 2>&1
```
Expected: `No projects to synthesize.`

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py src/juggle_cli.py skills/project:synthesis.md
git commit -m "feat(synth): project synth CLI command + skill stub (v1.45.0)"
```

---

## Phase 3 — Drift Detector + Silent Re-synthesis

**What:** `drift_score(centroid_vec, target_vec) -> float` computes cosine distance using stdlib `math` only. After each correction (`log_project_correction`), compute drift; if score > threshold, trigger `synth_project` async. Pure functions are fully unit-testable.

**Acceptance:** `drift_score` returns 0.0 for identical vectors, ~1.0 for orthogonal; `check_and_resynth_if_drifted` calls `synth_project` only when drift exceeds threshold; embeddings cached per-project.

> **Embedding note:** This phase uses the Hindsight embedding API (`HindsightClient`) if enabled, falling back to a TF-IDF-style bag-of-words centroid when Hindsight is unavailable. The drift check is purely for triggering re-synth — false negatives (missed drift) are acceptable; false positives (unnecessary synth) are cheap.

---

### Task 3.1 — `drift_score` pure function + project centroid

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_project_synth.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_project_synth.py`:

```python
def test_drift_score_identical_vectors_is_zero():
    from juggle_cmd_projects import drift_score
    v = [1.0, 0.5, 0.3]
    assert drift_score(v, v) == pytest.approx(0.0, abs=1e-6)


def test_drift_score_orthogonal_is_one():
    from juggle_cmd_projects import drift_score
    assert drift_score([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0, abs=1e-6)


def test_drift_score_handles_zero_vector():
    from juggle_cmd_projects import drift_score
    assert drift_score([0.0, 0.0], [1.0, 0.0]) == 1.0
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_project_synth.py::test_drift_score_identical_vectors_is_zero tests/test_project_synth.py::test_drift_score_orthogonal_is_one tests/test_project_synth.py::test_drift_score_handles_zero_vector -q
```

- [ ] **Step 3: Implement `drift_score` and `_topics_to_bow_vector`**

Add to `juggle_cmd_projects.py` (imports: add `import math` at top):

```python
import math


def drift_score(centroid: list[float], target: list[float]) -> float:
    """Cosine distance in [0, 1]: 0 = identical direction, 1 = orthogonal."""
    dot = sum(a * b for a, b in zip(centroid, target))
    mag_a = math.sqrt(sum(a * a for a in centroid))
    mag_b = math.sqrt(sum(b * b for b in target))
    if mag_a == 0.0 or mag_b == 0.0:
        return 1.0
    cosine_sim = dot / (mag_a * mag_b)
    return 1.0 - max(-1.0, min(1.0, cosine_sim))


def _topics_to_bow_vector(topics: list[str], vocab: dict[str, int]) -> list[float]:
    """Term-frequency bag-of-words vector over a shared vocabulary."""
    vec = [0.0] * len(vocab)
    for topic in topics:
        for word in topic.lower().split():
            if word in vocab:
                vec[vocab[word]] += 1.0
    total = sum(vec)
    return [x / total for x in vec] if total > 0 else vec


def _build_vocab(all_topics: list[str]) -> dict[str, int]:
    """Build word → index vocabulary from all topics."""
    words: list[str] = []
    seen: set[str] = set()
    for topic in all_topics:
        for word in topic.lower().split():
            if word not in seen and len(word) > 2:
                seen.add(word)
                words.append(word)
    return {w: i for i, w in enumerate(words)}
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_project_synth.py::test_drift_score_identical_vectors_is_zero tests/test_project_synth.py::test_drift_score_orthogonal_is_one tests/test_project_synth.py::test_drift_score_handles_zero_vector -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_project_synth.py
git commit -m "feat(drift): drift_score cosine distance + BoW centroid helpers"
```

---

### Task 3.2 — `check_and_resynth_if_drifted` wired into correction path

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_project_synth.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_project_synth.py`:

```python
def test_check_and_resynth_triggers_on_drift(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import check_and_resynth_if_drifted
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build software")
    # Add some threads to give the centroid something to work with
    for i in range(5):
        tid = db.create_thread(f"software task {i}", session_id="s1")
        db.update_thread(tid, project_id=pid, assigned_by="human")
    with patch("juggle_cmd_projects.synth_project") as mock_synth:
        # Force drift by patching drift_score to return high value
        with patch("juggle_cmd_projects.drift_score", return_value=0.9):
            check_and_resynth_if_drifted(db, pid, threshold=0.5)
    mock_synth.assert_called_once_with(db, pid)


def test_check_and_resynth_skips_below_threshold(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import check_and_resynth_if_drifted
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    with patch("juggle_cmd_projects.synth_project") as mock_synth:
        with patch("juggle_cmd_projects.drift_score", return_value=0.1):
            check_and_resynth_if_drifted(db, pid, threshold=0.5)
    mock_synth.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_project_synth.py::test_check_and_resynth_triggers_on_drift tests/test_project_synth.py::test_check_and_resynth_skips_below_threshold -q
```

- [ ] **Step 3: Implement `check_and_resynth_if_drifted`**

Add to `juggle_cmd_projects.py`:

```python
_DRIFT_DEFAULT_THRESHOLD = 0.45


def check_and_resynth_if_drifted(
    db, project_id: str, threshold: float = _DRIFT_DEFAULT_THRESHOLD
) -> None:
    """Compute topic-centroid drift for project_id. Silent re-synth if above threshold."""
    project = db.get_project(project_id)
    if not project:
        return
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT topic FROM threads WHERE project_id=? AND show_in_list=1 ORDER BY last_active DESC LIMIT 50",
            (project_id,),
        ).fetchall()
    topics = [r["topic"] for r in rows]
    if len(topics) < 3:
        return  # not enough signal
    profile_text = (project.get("match_profile") or "").strip()
    if not profile_text:
        return  # nothing to compare against

    # Build BoW centroid from actual threads vs. profile text
    profile_words = profile_text.split()
    vocab = _build_vocab(topics + [profile_text])
    thread_centroid = _topics_to_bow_vector(topics, vocab)
    profile_vec = _topics_to_bow_vector(profile_words, vocab)
    score = drift_score(thread_centroid, profile_vec)

    log.info("check_and_resynth_if_drifted: %s drift=%.3f threshold=%.3f", project_id, score, threshold)
    if score > threshold:
        log.info("check_and_resynth_if_drifted: drift detected, re-synth %s", project_id)
        synth_project(db, project_id)
```

Wire into `_assign_thread_to_project` (add after `mark_project_dirty`):

```python
def _assign_thread_to_project(
    db, thread_uuid: str, project_id: str, assigned_by: str = "human"
) -> None:
    t = db.get_thread(thread_uuid)
    if not t:
        return
    old_project = t.get("project_id", INBOX_PROJECT_ID)
    db.update_thread(thread_uuid, project_id=project_id, assigned_by=assigned_by)
    if old_project != project_id and old_project != INBOX_PROJECT_ID:
        db.mark_project_dirty(old_project)
        # Async drift check for old project (silent — no action-item nag)
        import threading
        threading.Thread(
            target=check_and_resynth_if_drifted,
            args=(db, old_project),
            daemon=True,
        ).start()
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_project_synth.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_project_synth.py
git commit -m "feat(drift): check_and_resynth_if_drifted; wire into assign path"
```

---

## Phase 4 — Confidence + INBOX Fallback + INBOX Re-sweep

**What:** Parse `confidence` from the LLM JSON response. Below threshold → INBOX (not a wrong project). After a profile changes, re-run matching on INBOX + low-confidence threads up to a rate-limited bound (50/run, configurable). Add `assigned_confidence` column on threads.

**Acceptance:** Thread with LLM confidence < 0.6 lands in INBOX; `resweep_inbox` re-classifies up to N INBOX threads per run and only runs after a profile was just updated; confidence stored on thread row.

---

### Task 4.1 — `assigned_confidence` column + classifier confidence output

**Files:**
- Modify: `src/juggle_db.py`
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_projects.py`

- [ ] **Step 1: Add Migration 31 for `assigned_confidence`**

In `juggle_db.py`, add Migration 31 after Migration 30:

```python
        # Migration 31: assigned_confidence on threads
        threads_cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "assigned_confidence" not in threads_cols:
            try:
                conn.execute(
                    "ALTER TABLE threads ADD COLUMN assigned_confidence REAL DEFAULT NULL"
                )
                conn.commit()
                _log.info("Migration 31: assigned_confidence added to threads")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 31 (assigned_confidence) skipped: %s", e)
```

- [ ] **Step 2: Update `infer_project_id` to return confidence and apply threshold**

Replace `infer_project_id` in `juggle_cmd_projects.py` (current lines 124–152):

```python
_CONFIDENCE_THRESHOLD = 0.6  # below this -> INBOX


def infer_project_id(
    topic: str,
    projects: list[dict],
    db=None,
    confidence_threshold: float = _CONFIDENCE_THRESHOLD,
) -> tuple[str, float]:
    """Returns (project_id, confidence). Falls back to (INBOX, 0.0) on any failure.

    Confidence < threshold -> INBOX regardless of project_id returned.
    """
    if not projects:
        return INBOX_PROJECT_ID, 0.0
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}

    positives_by_project: dict[str, list[dict]] = {}
    corrections: list[dict] = []
    if db:
        try:
            for p in projects:
                positives_by_project[p["id"]] = db.get_human_assigned_threads_by_project(p["id"], limit=5)
        except Exception:
            pass
        try:
            corrections = db.get_recent_corrections(limit=5)
        except Exception:
            pass

    prompt = _build_classifier_prompt(topic, projects, positives_by_project, corrections)
    raw = llm_call(prompt, profile="cheap", timeout=15)
    if not raw:
        return INBOX_PROJECT_ID, 0.0
    parsed = _extract_json(raw) or {}
    pid = parsed.get("project_id", INBOX_PROJECT_ID)
    confidence = float(parsed.get("confidence", 0.5))
    if pid not in valid_ids:
        log.warning("infer_project_id: invalid project_id %r in response: %r", pid, raw)
        return INBOX_PROJECT_ID, 0.0
    if confidence < confidence_threshold and pid != INBOX_PROJECT_ID:
        log.info("infer_project_id: low confidence %.2f for %r -> INBOX", confidence, pid)
        return INBOX_PROJECT_ID, confidence
    return pid, confidence
```

Update `assign_project_background` to unpack the tuple and store confidence:

```python
# In the _run() inner function inside assign_project_background:
project_id, confidence = infer_project_id(topic, projects, db=db)
if project_id != INBOX_PROJECT_ID:
    db.update_thread(thread_uuid, project_id=project_id, assigned_by="auto",
                     assigned_confidence=confidence)
else:
    # Store even INBOX assignments with confidence for re-sweep priority
    db.update_thread(thread_uuid, assigned_confidence=confidence)
```

Also update the detached subprocess script in `assign_project_background` to handle the tuple:

```python
script = (
    "import sys; sys.path.insert(0, {src!r}); "
    "from juggle_db import JuggleDB, DB_PATH; "
    "from juggle_cmd_projects import infer_project_id, INBOX_PROJECT_ID; "
    "db = JuggleDB(str(DB_PATH)); "
    "projects = db.get_active_projects(); "
    "pid, conf = infer_project_id({topic!r}, projects, db=db); "
    "pid != INBOX_PROJECT_ID and db.update_thread({thread_uuid!r}, project_id=pid, assigned_by='auto', assigned_confidence=conf) "
    "or db.update_thread({thread_uuid!r}, assigned_confidence=conf)"
).format(src=str(SRC_DIR), topic=topic, thread_uuid=thread_uuid)
```

- [ ] **Step 3: Write failing tests**

Add to `tests/test_projects.py`:

```python
def test_infer_low_confidence_returns_inbox():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call",
               return_value='{"project_id": "P1", "confidence": 0.3}'):
        pid, conf = infer_project_id("ambiguous topic", PROJECTS)
    assert pid == "INBOX"
    assert conf == pytest.approx(0.3)


def test_infer_high_confidence_returns_project():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call",
               return_value='{"project_id": "P1", "confidence": 0.9}'):
        pid, conf = infer_project_id("automate investing ideas", PROJECTS)
    assert pid == "P1"
    assert conf == pytest.approx(0.9)


def test_infer_returns_tuple():
    from juggle_cmd_projects import infer_project_id
    with patch("juggle_cmd_projects.llm_call", return_value=None):
        result = infer_project_id("some topic", PROJECTS)
    assert isinstance(result, tuple)
    assert result == ("INBOX", 0.0)
```

> **Compatibility note:** Existing tests mock `_cheap_llm_call`; after this change they'll need to mock `juggle_cmd_projects.llm_call` instead. Update all existing `test_projects.py` mocks from `_cheap_llm_call` to `llm_call` in this step.

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_projects.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_db.py src/juggle_cmd_projects.py tests/test_projects.py
git commit -m "feat(classifier): confidence output + INBOX fallback below threshold; migration 31"
```

---

### Task 4.2 — INBOX re-sweep

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `tests/test_project_synth.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_project_synth.py`:

```python
def test_resweep_inbox_reclassifies_unassigned(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import resweep_inbox
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "Build software")
    tid = db.create_thread("software task", session_id="s1")
    # Leave thread in INBOX (default)
    assert db.get_thread(tid)["project_id"] == "INBOX"
    with patch("juggle_cmd_projects.infer_project_id", return_value=(pid, 0.85)):
        resweep_inbox(db, limit=10)
    assert db.get_thread(tid)["project_id"] == pid


def test_resweep_inbox_respects_limit(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import resweep_inbox
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.create_project("Dev", "Build software")
    for i in range(10):
        db.create_thread(f"task {i}", session_id="s1")
    call_count = 0
    def fake_infer(topic, projects, db=None, **kw):
        nonlocal call_count
        call_count += 1
        return ("INBOX", 0.3)
    with patch("juggle_cmd_projects.infer_project_id", side_effect=fake_infer):
        resweep_inbox(db, limit=5)
    assert call_count == 5
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_project_synth.py::test_resweep_inbox_reclassifies_unassigned tests/test_project_synth.py::test_resweep_inbox_respects_limit -q
```

- [ ] **Step 3: Implement `resweep_inbox`**

Add to `juggle_cmd_projects.py`:

```python
_RESWEEP_DEFAULT_LIMIT = 50


def resweep_inbox(db, limit: int = _RESWEEP_DEFAULT_LIMIT) -> int:
    """Re-run project matching on INBOX threads. Returns count reclassified.

    Rate-limited to `limit` threads per call. Called automatically after a
    profile is updated (see synth_project). Does nothing if no active projects.
    """
    projects = db.get_active_projects()
    if not projects:
        return 0
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, topic FROM threads "
            "WHERE project_id='INBOX' AND show_in_list=1 "
            "ORDER BY COALESCE(assigned_confidence, 0.0) ASC, last_active DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
    reclassified = 0
    for row in rows:
        pid, confidence = infer_project_id(row["topic"], projects, db=db)
        if pid != INBOX_PROJECT_ID:
            db.update_thread(row["id"], project_id=pid, assigned_by="auto",
                             assigned_confidence=confidence)
            reclassified += 1
            log.info("resweep_inbox: %s -> %s (conf=%.2f)", row["id"][:8], pid, confidence)
    return reclassified
```

Wire `resweep_inbox` into `synth_project` after a successful synthesis:

```python
# At end of synth_project(), after db.set_match_profile():
db.set_match_profile(project_id, result.strip())
log.info("synth_project: synthesized profile for %s", project_id)
# Trigger bounded re-sweep to recover misrouted INBOX threads
import threading
threading.Thread(
    target=resweep_inbox,
    args=(db,),
    kwargs={"limit": _RESWEEP_DEFAULT_LIMIT},
    daemon=True,
).start()
return result.strip()
```

- [ ] **Step 4: Run all project tests**

```bash
uv run pytest tests/test_project_synth.py tests/test_projects.py tests/test_projects_db.py -q
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_cmd_projects.py tests/test_project_synth.py
git commit -m "feat(sweep): resweep_inbox reclassifies INBOX threads after profile update"
```

---

## Phase 5 — `project assign` Fixes: Archived Threads + Bulk IDs

**What:** Fix `project assign` to work on archived/closed threads (currently "Thread not found"). Support `project assign <id1> <id2> ... <projectId>` syntax for bulk reassignment.

**Acceptance:** `project assign AA P2` works when AA is archived; `project assign AA BB CC P2` reassigns all three.

> Note: Task 2.2 already updated `cmd_project_assign` partially. This task completes the CLI argument parser changes and tightens the implementation.

---

### Task 5.1 — Finalize bulk + archived support in `cmd_project_assign`

**Files:**
- Modify: `src/juggle_cmd_projects.py`
- Modify: `src/juggle_cli.py`
- Modify: `tests/test_projects.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_projects.py`:

```python
import types

def _make_args(**kw):
    ns = types.SimpleNamespace()
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_cmd_project_assign_archived_thread(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    tid = db.create_thread("archived task", session_id="s1")
    db.archive_thread(tid)  # mark archived
    t = db.get_thread(tid)
    label = t["user_label"]
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(_make_args(thread_id=[label], project_id=pid))
    assert db.get_thread(tid)["project_id"] == pid


def test_cmd_project_assign_bulk(tmp_path):
    from juggle_db import JuggleDB
    from juggle_cmd_projects import cmd_project_assign
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    pid = db.create_project("Dev", "obj")
    t1 = db.create_thread("task one", session_id="s1")
    t2 = db.create_thread("task two", session_id="s1")
    l1 = db.get_thread(t1)["user_label"]
    l2 = db.get_thread(t2)["user_label"]
    with patch("juggle_cmd_projects.get_db", return_value=db):
        cmd_project_assign(_make_args(thread_id=[l1, l2], project_id=pid))
    assert db.get_thread(t1)["project_id"] == pid
    assert db.get_thread(t2)["project_id"] == pid
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_projects.py::test_cmd_project_assign_archived_thread tests/test_projects.py::test_cmd_project_assign_bulk -q
```

- [ ] **Step 3: Update CLI parser for bulk thread_id**

In `juggle_cli.py`, change the `assign` subparser to accept multiple thread_ids:

```python
    _p = _ps.add_parser("assign")
    _p.add_argument("thread_id", nargs="+", help="One or more thread labels/UUIDs")
    _p.add_argument("project_id")
    _p.set_defaults(func=cmd_project_assign)
```

> **Parser note:** With `nargs="+"`, the last positional arg is still `project_id` but argparse will greedily consume all into `thread_id`. Restructure to use an explicit `--project`/`-p` flag instead, OR keep current convention `assign <threads...> <project>` by doing `args.project_id = args.thread_id[-1]; args.thread_id = args.thread_id[:-1]` at the top of `cmd_project_assign`.

Implement the split at the top of `cmd_project_assign`:

```python
def cmd_project_assign(args):
    db = get_db(init=True)
    # Unpack: last positional is always project_id
    if isinstance(args.thread_id, list) and len(args.thread_id) > 1:
        project_id = args.thread_id[-1]
        thread_ids = args.thread_id[:-1]
    else:
        project_id = args.project_id
        thread_ids = args.thread_id if isinstance(args.thread_id, list) else [args.thread_id]

    p = db.get_project(project_id)
    if not p:
        print(f"Project not found: {project_id}")
        sys.exit(1)

    for tid_input in thread_ids:
        # Try active threads first
        t = db.get_thread_by_user_label(tid_input)
        if not t:
            # Fall through to archived/closed threads
            with db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM threads WHERE user_label=? OR id=?",
                    (tid_input.upper(), tid_input),
                ).fetchone()
            t = dict(row) if row else None
        if not t:
            print(f"Thread not found: {tid_input}")
            continue
        from_project = t.get("project_id", "INBOX")
        _assign_thread_to_project(db, t["id"], project_id, assigned_by="human")
        if from_project != project_id:
            db.log_project_correction(t["topic"], from_project=from_project, to_project=project_id)
        print(f"Thread [{tid_input}] -> project {project_id} ({p['name']})")
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
uv run pytest tests/test_projects.py -q
```

- [ ] **Step 5: Run full suite**

```bash
uv run pytest tests/ -q --ignore=tests/test_juggle_hooks.py -x
```

- [ ] **Step 6: Bump version and commit**

In `.claude-plugin/plugin.json`, bump minor version (e.g. `1.44.x` → `1.45.0` if not already done in Phase 2).

```bash
git add src/juggle_cmd_projects.py src/juggle_cli.py tests/test_projects.py .claude-plugin/plugin.json
git commit -m "feat(assign): bulk + archived thread support for project assign (v1.45.0)"
```

---

## Self-Review

### Spec Coverage Check

| Spec requirement | Task |
|-----------------|------|
| Separate `match_profile` field, migration, never overwrite `objective` | Task 1.1 |
| `/juggle:project:synthesis` slash command + `project synth [--all\|--dirty\|<id>]` | Task 2.3 |
| Dirty-tracking on corrections/reassignments | Task 2.2, Task 3.2 |
| Drift detector → silent re-synth (no action-item nag) | Task 3.2 |
| Trigger = manual command + silent-on-drift (NOT periodic cron) | Task 3.2 |
| Input weighting: human > auto, bounded ~30 titles | Task 2.1 |
| Output: paragraph + keywords + negative keywords | Task 2.1 (prompt) |
| Cost control: dirty-tracking, skip unchanged projects | Task 2.2 |
| Two LLM profiles: cheap/normal with OpenRouter+fallback | Phase 0 |
| Profile model IDs editable in config.json | Task 0.1 |
| Existing call sites default to "cheap" | Task 0.2 (shim) |
| Low-confidence → INBOX (not wrong project) | Task 4.1 |
| Confidence threshold configurable | Task 4.1 (constant, editable) |
| INBOX re-sweep after profile change, rate-limited | Task 4.2 |
| `project assign` works on archived threads | Task 5.1 |
| Bulk reassign `project assign <id...>` | Task 5.1 |
| Unit tests with LLM mocked | All tasks |
| Pure functions at testable seams | Task 2.1, 3.1 |

### No-Placeholder Scan

- All test code is complete and runnable.
- All implementation code is complete (no "add error handling" stubs).
- Type signatures are consistent: `infer_project_id` returns `tuple[str, float]` throughout.

### Type Consistency

- `infer_project_id` returns `(str, float)` in Task 4.1 — all callers updated in same task.
- `_assign_thread_to_project` introduced in Task 2.2 and reused in Tasks 3.2, 4.1, 5.1 with identical signature.
- `llm_call(prompt, profile, timeout)` introduced Task 0.2, used in Tasks 2.2, 4.1.
- `synth_project(db, project_id, force=False)` introduced Task 2.2, called in Tasks 3.2, 4.2.

---

## Devil's Advocate

### 1. Feedback-loop reinforcement
**Risk:** Auto-assigned threads feed the BoW centroid and the synthesis prompt, causing the profile to drift toward already-assigned content rather than the project's true meaning.

**Mitigation:** `build_match_profile_prompt` explicitly labels auto-assigned threads "use lightly" and human-assigned "trust these most" — the LLM is directly instructed about trust ranking. Auto sample is bounded at 10 vs. 20 human. The synthesis prompt also includes topics moved OUT (corrections), which act as negative signal. Residual risk: if a project has zero human assignments, only auto threads exist — synth will be weak. Acceptable: INBOX fallback prevents catastrophic mis-routing.

### 2. YAGNI — Matcher already worked after manual fix
**Risk:** The existing classifier was 6/6 correct after the 16-thread manual SQL fix. Adding synthesis, drift, and re-sweep is over-engineering.

**Counter:** The root cause of the P1/P2 incident was stale `objective` (finance goal vs. software threads). The fix was manual SQL — not a sustainable workflow for 16 threads let alone 405 INBOX threads. `match_profile` earns its cost specifically because it's synthesized from actual thread content, not human-written intent. The drift detector is the automatic maintenance path that prevents the stale-objective scenario from recurring silently. These features don't replace the classifier — they feed it better inputs.

### 3. Re-sweep churn bounding
**Risk:** 405-thread INBOX backlog could trigger expensive LLM calls (405 × cheap model = non-trivial cost/time).

**Mitigation:** `resweep_inbox` is bounded at 50 threads per invocation (configurable constant `_RESWEEP_DEFAULT_LIMIT`). INBOX threads sorted ascending by `assigned_confidence` so lowest-confidence (most likely to reclassify) are processed first. Additional invocations needed for full backlog — which is fine, each profile change only sweeps 50.

### 4. Embedding cost (BoW vs. real embeddings)
**Risk:** BoW drift detection has low precision — many false positives (unnecessary synth) or false negatives (missed drift).

**Mitigation:** False positives (unnecessary synth) cost ~one cheap LLM call — acceptable. False negatives (missed drift) just mean the manual `project synth` command remains available. The BoW approach requires zero additional API calls. If Hindsight is enabled, the architecture naturally supports swapping in real embeddings in a single function (`_topics_to_bow_vector`) without changing the rest of the system.

### 5. `deepseek/deepseek-chat-v3-0324:free` model ID availability
**Risk:** OpenRouter model slugs change; the free tier may be withdrawn; the exact ID in defaults may be stale by deploy time.

**Mitigation:** Model IDs live in `llm_profiles` in `~/.juggle/config.json` (user-editable, no code change). Default is annotated "verify at openrouter.ai/models". Failure path: OpenRouter call fails → fallback to Haiku. The system is resilient to model unavailability.

### 6. Confidence threshold calibration
**Risk:** Default threshold 0.6 may be too high (floods INBOX) or too low (allows mis-assignments).

**Mitigation:** The spec deliberately biases toward INBOX-over-misfile. INBOX threads are recoverable via re-sweep; mis-assignments require user intervention. Threshold is a constant (`_CONFIDENCE_THRESHOLD = 0.6`) editable without touching logic. Observability: `assigned_confidence` is stored on every thread row — a simple SQL query reveals the confidence distribution for calibration.

### 7. `infer_project_id` return type change breaks callers
**Risk:** Changing `infer_project_id` from `str` to `tuple[str, float]` breaks the detached subprocess script in `assign_project_background` and any external callers.

**Mitigation:** Phase 4 Task 4.1 explicitly updates the subprocess script inline. Internal callers (`assign_project_background`, `cmd_project_assign` via `_assign_thread_to_project`) are all in `juggle_cmd_projects.py` — updated in same task. Tests catch regressions.
