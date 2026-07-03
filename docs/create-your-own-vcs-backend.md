# Create your own VCS backend

**Audience:** a fresh Claude Code session with **zero juggle context** —
possibly on a machine that has never seen this repo before (e.g. a Meta
devserver). You've been told: *"implement and set up a new VCS backend for
this repo."* This one file is everything you need. You should not have to
read juggle's source to finish the job.

**Ships in the juggle repo.** Unlike the design spec this guide implements
(which lives outside the repo), this file is the durable, versioned contract
your plugin is written against. It stays in sync with the `VCS` Protocol via
an automated test (`tests/test_vcs_protocol_sync.py`) — if a method here looks
stale, the sync test would have already failed juggle's CI, so trust this file.

---

## 0. The shape of the job

juggle drives dispatch → integrate → verify against a repo through **one**
seam: `src/vcs.py`'s `VCS` Protocol. Git ships in-tree. Every other VCS
(Sapling/`sl`, Mercurial-proper, jujutsu, whatever) ships as a **drop-in
plugin file** juggle loads at runtime — no changes to the juggle repo itself.

You will:

1. Write one Python module implementing the 15 methods below (§1).
2. Drop it at `~/.juggle/vcs_plugins/<name>.py` (a symlink is fine) with three
   required top-level symbols (§2).
3. Point a repo at it via config, and confirm resolution with a one-liner (§3).
4. Run the exported conformance kit against it — this is the **done-gate**,
   not optional (§4).
5. Start from the worked skeleton (§5) rather than from scratch.
6. Read the non-goals (§6) before you write a line of code that touches
   juggle's DB, migrations, or git-specific assumptions.

---

## 1. The Protocol contract — every method, in full

`Rev` and workspace paths are **opaque, str-backed values**. Store, compare,
pass them — never parse them or assume sha/branch-name shape. Read-only
queries below are **best-effort**: return `None`/`""`/`[]`/`False` on any
internal tool error, never raise. The two mutators that can fail
*meaningfully* (`update_to`, `submit`) return rich result objects instead —
juggle's pipeline inspects those to decide whether to fail loud.

### Identity & detection

| Method | Signature | Semantics | Error contract | Called by |
|---|---|---|---|---|
| `name` | `str` (attribute) | Backend id (`"git"`, `"sapling"`, …) — orchestrator NEVER branches on this string; it only logs/displays it. | n/a | logging, `backend_for` cache key disambiguation |
| `capabilities` | `Capabilities` (attribute) | Frozen flags: `async_land: bool`, `auto_restack: bool`. A new backend quirk becomes a flag here, never a new method. | n/a | integrate policy (async-land branching, Phase 2) |
| `repo_root(path)` | `-> str \| None` | Root of the enclosing repo from any subdirectory. | `None` if `path` isn't inside a repo | repo-binding resolution, `juggle_cmd_graph` toplevel checks |
| `primary_root(repo)` | `-> str` | The primary checkout of a repo that may itself be a linked workspace (e.g. a git worktree, or an `eden clone`). | best-effort fallback to `repo` itself if unresolvable | worktree naming (nested-dispatch safety), `backend_for`'s cache key |

### History facts (read-only, repo-scoped)

| Method | Signature | Semantics | Error contract | Called by |
|---|---|---|---|---|
| `refresh(repo)` | `-> None` | Make subsequent history queries reflect remote reality (git: `fetch --prune`). Non-fatal for remoteless repos. | never raises; silent no-op on failure | integrate pipeline step 1 |
| `trunk(repo)` | `-> Rev \| None` | Current canonical trunk tip/ref. | `None` if no trunk can be resolved | integrate pipeline step 2 (rebase/update target); `stack_base` (Phase 2) |
| `resolve(repo, ref=None)` | `-> Rev \| None` | `ref` → rev. `ref=None` → tip of the primary checkout (used for the watchdog's stale-code fingerprint). Subsumes existence checks — an unresolvable ref is `None`, not an exception. | `None` on unknown/nonexistent ref | `resolve_branch_sha`, `canonical_main_ref`, watchdog fingerprinting, ledger `head` alias |
| `is_ancestor(repo, rev, of)` | `-> bool` | `rev` ⊑ `of`. **THE verified-gate primitive.** | **fail-closed**: ANY error → `False`, never `True` on doubt | `topic_is_merged` — the single source of truth for "verified" |

### Workspace lifecycle

| Method | Signature | Semantics | Error contract | Called by |
|---|---|---|---|---|
| `create_workspace(repo, name, root, *, base=None)` | `-> WorkspaceResult` | Isolated workspace forked at `base` (git: `worktree add -b`). `name`/`root` are ALREADY-COMPUTED by the caller (naming conventions like `cyc_<label>` never live in your backend). | `WorkspaceResult(ok=False, ...)` with `.detail` on failure — never raise | dispatch worktree auto-create |
| `remove_workspace(repo, ws)` | `-> bool` | Full teardown **including** branch/bookmark/stack cleanup — that bookkeeping is backend-private. | `False` on failure, never raise | integrate cleanup, `_finalize_worktree` |
| `current_rev(ws)` | `-> Rev \| None` | Workspace tip. | `None` on error | conflict/rebase bookkeeping |
| `dirty_files(ws)` | `-> list[str]` | Uncommitted changes (staged or unstaged). Truthiness = dirty. | `[]` on error (never used to mean "clean" vs a real error — callers only check truthiness) | integrate's G1 dirty gate |
| `has_changes(ws, *, since)` | `-> bool` | "Does this workspace contain any work relative to `since`?" — the empty-work guard's actual intent. | on tool error, return `True` (assume work exists — conservative, never silently discards work) | integrate's G2 empty-branch guard |
| `update_to(ws, base)` | `-> UpdateResult` | True the workspace up onto `base`, **including** idempotent recovery from a previously interrupted update (an in-progress rebase/merge is auto-aborted before retrying — never left half-done). | `UpdateResult(ok=False, conflicts=[...])` on conflict — never raise, never leave the workspace mid-operation | integrate pipeline step 4 |
| `describe_changes(ws, *, since)` | `-> str` | Best-effort human-readable change summary (diffstat-shaped) for completion messages. | `""` is a fine answer | task diffstat capture (hydration, non-gating) |

### Publish

| Method | Signature | Semantics | Error contract | Called by |
|---|---|---|---|---|
| `submit(ws, *, base, mode, push=True)` | `-> SubmitResult` | Get the workspace's tested work onto (or into the queue toward) trunk. `mode="direct"` → merges/lands locally, `push=True` also publishes and returns `status="landed"` + `landed_rev`; `push=False` merges locally only (juggle's `push_mode="none"`). `mode="queue"` → pushes to a PR/land-queue, returns `status="submitted"` + an opaque `ticket`. Encapsulates ALL of your VCS's local-integration mechanics (git: local-main-sync → ff-merge → push) — that recipe is backend-private, never orchestrator policy. | `SubmitResult(status="failed", detail=...)` — never raise | integrate pipeline step 5 (the merge/publish step) |
| `land_status(repo, ticket)` | `-> LandStatus` | **The land-poller's single question:** has this submission landed? `state ∈ "landed"\|"unlanded"\|"failed"` + `landed_rev`. Submitted→landed identity resolution (mutation-tracking successors, squash-merge PR state, whatever your VCS's async-land mechanism needs) lives **entirely inside your backend** — the orchestrator never sees the mechanism. | **fail-closed**: unknown/ambiguous → `"unlanded"`, never `"landed"` on doubt (a false "landed" is the one unrecoverable mistake — Phase 2's land-poller only promotes a topic to `verified` on this answer) | Phase 2 land-poller (async-land backends only; git-direct never reaches this) |

### Back-compat aliases (pre-widening ledger surface — implement, but treat as thin wrappers)

| Method | Signature | Semantics |
|---|---|---|
| `head(path)` | `-> str \| None` | Alias for `resolve(path, None)`. |
| `is_dirty(path)` | `-> bool` | Alias for `bool(dirty_files(path))`. |
| `make_safety_branch(path, sha, name)` | `-> bool` | Create+checkout a named branch/bookmark at `sha` (git: `branch` + `switch`). Used only by the runs-ledger restore path — not part of the integrate pipeline. |

---

## 2. Plugin file contract

Drop a **single Python module** at:

```
~/.juggle/vcs_plugins/<name>.py
```

(A symlink is fine — keep the real file in your own plugin repo and symlink
it in. juggle's loader follows symlinks transparently.)

The file must define exactly three top-level symbols:

```python
BACKEND: MyBackend        # an INSTANCE implementing every method in §1 —
                           # structural typing means ZERO juggle imports are
                           # required; just implement the method names.
PROTOCOL_VERSION = 1      # REQUIRED. Must equal juggle's current
                           # VCS_PROTOCOL_VERSION (src/vcs.py) or the loader
                           # REJECTS the plugin (fail-loud, never a silent skip).

def detect(repo_root: str) -> bool:   # OPTIONAL
    """True iff this backend owns repo_root."""
    ...
```

- `BACKEND.name` — a short string id (`"sapling"`, `"jj"`, …).
- `detect()` is optional; when present it's consulted during auto-detection
  (§3), scanned only AFTER juggle's builtin git/hg detection misses.
- A version mismatch, a missing `BACKEND`/`PROTOCOL_VERSION`, or any
  import/exec error is a **hard failure** (a HIGH action item), never a
  silent fallback to git — a silently-absent backend on a `.sl` repo would
  corrupt the wrong VCS.

---

## 3. Setup / config — binding a repo to your backend

Explicit binding (skips `detect()` entirely):

```json
// ~/.juggle/config.json
{
  "repos": {
    "/path/to/your/repo": {
      "vcs": "sapling",
      "push_mode": "direct",
      "test_cmd": "your test command"
    }
  }
}
```

Resolution order (`vcs.backend_for(repo)`, `src/vcs.py`):

1. **Explicit `vcs` config override** for that exact repo path — binds
   directly to the plugin whose `BACKEND.name` matches.
2. **Builtin auto-detect** — git (`.git`) first, then the hg stub. (Git-first
   is deliberate: a repo with both `.git` and `.sl` is treated as git.)
3. **Plugin `detect()` scan** — only reached if step 2 found nothing.
4. **Cached** per resolved `primary_root` for the process lifetime.

**Confirm your repo actually binds to your backend before trusting anything
else** — a one-liner probe:

```bash
cd /path/to/juggle
uv run python -c "
import sys; sys.path.insert(0, 'src')
from vcs import backend_for
print(backend_for('/path/to/your/repo').name)
"
```

If this doesn't print your backend's `name`, nothing downstream will work —
fix resolution before writing a single test against the conformance kit.

---

## 4. Verification workflow (AGENT-FIRST — this is the done-gate)

**A backend is not "done" until `vcs_conformance.py` passes against it — on
the machine where your VCS actually runs.** juggle's own CI only runs the kit
over `GitVCS` + an in-memory `FakeBackend`; there is no Sapling (or any other
non-git backend) in juggle's CI. Verification for your backend happens in
**your** plugin repo, against the real tool.

In your plugin repo, import juggle's kit and supply your own harness fixture:

```python
# your_plugin_repo/tests/test_conformance.py
import sys
sys.path.insert(0, "/path/to/juggle/src")  # or however you vendor it

from vcs_conformance import *  # re-exports every test_* function

import pytest
from my_backend import BACKEND

class MyHarness:
    def __init__(self, tmp_path):
        self.vcs = BACKEND
        # ... implement new_repo() / new_workspace_path() / commit() /
        # dirty() / advance_trunk() / conflicting_advance_trunk() /
        # leave_interrupted_update() against REAL `sl` (or your VCS) ...

@pytest.fixture
def conformance_harness(tmp_path):
    return MyHarness(tmp_path)
```

Run it:

```bash
cd your_plugin_repo
uv run pytest -q tests/test_conformance.py
```

**What green means:** every Protocol method (§1) behaves correctly against a
real throwaway repo — workspace create/remove, resolve/is_ancestor, an actual
rebase-equivalent (including a genuine conflict AND recovery from an
interrupted one), dirty/has-changes detection, and a full
`submit(mode="direct")` → `land_status` lifecycle. This is what "correct" means
for a VCS backend in juggle's sense — nothing else is required, and nothing
less is sufficient. Ship it once this is green on the target machine.

---

## 5. Worked minimal example (skeleton to start from)

```python
"""~/.juggle/vcs_plugins/example.py — a trivial reference skeleton.

Copy this, rename the class, and replace every `...` with real calls to your
VCS's CLI. Delete nothing from the method list in §1 — the Protocol is
structural, but juggle's pipeline calls every one of these at some point.
"""
import subprocess


def _run(args, cwd):
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


class Capabilities:
    def __init__(self, async_land, auto_restack):
        self.async_land = async_land
        self.auto_restack = auto_restack


class ExampleBackend:
    name = "example"
    capabilities = Capabilities(async_land=True, auto_restack=True)

    def repo_root(self, path):
        return _run(["myvcs", "root"], path)

    def primary_root(self, repo):
        return _run(["myvcs", "primary-checkout"], repo) or repo

    def refresh(self, repo):
        _run(["myvcs", "pull"], repo)

    def trunk(self, repo):
        return _run(["myvcs", "log", "-r", "remote/main", "-T", "{node}"], repo)

    def resolve(self, repo, ref=None):
        if ref is None:
            return _run(["myvcs", "whereami"], repo)
        return _run(["myvcs", "log", "-r", ref, "-T", "{node}"], repo)

    def is_ancestor(self, repo, rev, of):
        if not repo or not rev or not of:
            return False
        out = _run(["myvcs", "log", "-r", f"ancestor({rev},{of})", "-T", "{node}"], repo)
        return out == rev

    def create_workspace(self, repo, name, root, *, base=None):
        ...  # e.g. `myvcs clone` / `eden clone` into `root` at `base`

    def remove_workspace(self, repo, ws):
        ...  # teardown INCLUDING branch/bookmark cleanup

    def current_rev(self, ws):
        return _run(["myvcs", "whereami"], ws)

    def dirty_files(self, ws):
        out = _run(["myvcs", "status"], ws)
        return out.splitlines() if out else []

    def has_changes(self, ws, *, since):
        out = _run(["myvcs", "log", "-r", f"{since}::. - {since}", "-T", "{node}\\n"], ws)
        return bool(out)

    def update_to(self, ws, base):
        ...  # rebase-equivalent + interrupted-update self-recovery

    def describe_changes(self, ws, *, since):
        return _run(["myvcs", "diff", "--stat", "-r", since], ws) or ""

    def submit(self, ws, *, base, mode, push=True):
        ...  # mode="direct": local land; mode="queue": land-queue submit

    def land_status(self, repo, ticket):
        ...  # resolve submitted->landed identity INSIDE the backend

    def head(self, path):
        return self.resolve(path, None)

    def is_dirty(self, path):
        return bool(self.dirty_files(path))

    def make_safety_branch(self, path, sha, name):
        ...


BACKEND = ExampleBackend()
PROTOCOL_VERSION = 1


def detect(repo_root: str) -> bool:
    return _run(["myvcs", "root"], repo_root) is not None
```

---

## 6. Explicit non-goals

Your backend must **NOT**:

- **Touch the shared production juggle DB.** Your plugin has no business
  opening `~/.claude/juggle/juggle.db` — it only implements git-mechanics
  primitives; state lives entirely in juggle's orchestrator, not your plugin.
- **Run juggle DB migrations.** Ever. That's exclusively orchestrator code.
- **Assume git semantics.** Don't assume `Rev` values are 40-char hex shas,
  don't assume `is_ancestor` can be computed via a generic "merge-base"-style
  operation if your VCS's mutation model doesn't support that directly — use
  your VCS's own primitives (e.g. Sapling's mutation-tracking `successors()`)
  and expose the ANSWER through `is_ancestor`/`land_status`, not the mechanism.
- **Assume squash/rebase-on-land preserves identity.** If your VCS's land
  step rewrites the submitted commit (squash-merge, rebase-on-land), resolve
  that identity mapping **inside `land_status`** — this is precisely the
  seam that exists so the orchestrator's land-poller never needs to know.
- **Sandbox yourself as more trusted than you are.** The plugin dir sits at
  the same trust level as juggle itself (juggle already runs your `test_cmd`
  and dispatched agents with your full privileges) — but that cuts both
  ways: don't reach for capabilities juggle's own code doesn't have either.
