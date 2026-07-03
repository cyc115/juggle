"""vcs_conformance — shared VCS Protocol conformance kit (SPEC 2026-07-02 §2.5.3).

A backend is "correct" iff it passes every test in this module. This is the
ONE artifact both sides of the seam run: juggle main CI parametrizes it over
`GitVCS` + `FakeBackend` (this repo's tests/test_vcs_conformance.py) — no
Sapling in main CI. An out-of-tree plugin repo imports this SAME module and
parametrizes it over its real backend (e.g. Sapling against live `sl`) as its
own gate, run where that tool actually exists.

Contract: every test function below depends on a fixture named
``conformance_harness`` — a duck-typed object the CALLING test file supplies
(not defined here) exposing:

    .vcs                              the VCS instance under test
    .new_repo() -> str                a fresh throwaway repo/root path
    .commit(ws, name, content) -> None   commit a file into workspace ``ws``
    .dirty(ws, name, content) -> None    write an UNCOMMITTED file into ``ws``

Juggle's own harness (tests/test_vcs_conformance.py) implements this for
GitVCS with real tmp git repos, and for FakeBackend by scripting consistent
return values (FakeBackend has no real filesystem — conformance there
verifies the Protocol *shape*, not git semantics).

Coverage (§2.5.3): workspace create/remove, commit, resolve/is_ancestor,
update_to (incl. conflict + interrupted-update recovery), has_changes,
dirty_files, submit(direct) -> land_status lifecycle.
"""

from __future__ import annotations


def test_repo_root_and_primary_root(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()

    assert h.vcs.repo_root(repo) is not None
    assert h.vcs.primary_root(repo) is not None


def test_refresh_does_not_raise(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()

    h.vcs.refresh(repo)  # non-fatal, best-effort — must not raise


def test_create_and_remove_workspace(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()

    result = h.vcs.create_workspace(repo, "conformance-branch", ws, base=base)
    assert result.ok is True
    assert result.path == ws
    assert result.branch == "conformance-branch"

    assert h.vcs.remove_workspace(repo, ws) is True


def test_commit_advances_current_rev(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "commit-branch", ws, base=base)

    before = h.vcs.current_rev(ws)
    h.commit(ws, "a.txt", "one\n")
    after = h.vcs.current_rev(ws)

    assert after is not None
    assert after != before


def test_resolve_and_is_ancestor(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "ancestor-branch", ws, base=base)
    h.commit(ws, "a.txt", "one\n")
    tip = h.vcs.current_rev(ws)

    assert h.vcs.resolve(ws) == tip
    assert h.vcs.is_ancestor(ws, base, tip) is True


def test_is_ancestor_fails_closed_on_bad_repo(conformance_harness):
    h = conformance_harness
    assert h.vcs.is_ancestor("", "deadbeef", "main") is False


def test_dirty_files_reflects_workspace_state(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "dirty-branch", ws, base=base)

    assert h.vcs.dirty_files(ws) == []
    h.dirty(ws, "scratch.txt", "wip\n")
    assert h.vcs.dirty_files(ws) != []


def test_has_changes_false_then_true(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "haschanges-branch", ws, base=base)

    since = h.vcs.current_rev(ws)
    assert h.vcs.has_changes(ws, since=since) is False
    h.commit(ws, "new.txt", "new\n")
    assert h.vcs.has_changes(ws, since=since) is True


def test_update_to_advances_workspace(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "update-branch", ws, base=base)
    h.commit(ws, "feature.txt", "feat\n")
    new_base = h.advance_trunk(repo, "trunk_advance.txt", "advanced\n")

    result = h.vcs.update_to(ws, new_base)

    assert result.ok is True
    assert result.conflicts == []
    assert h.vcs.is_ancestor(ws, new_base, h.vcs.current_rev(ws)) is True


def test_update_to_conflict_reports_and_recovers(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "conflict-branch", ws, base=base)
    h.commit(ws, "shared.txt", "workspace-side\n")
    new_base = h.conflicting_advance_trunk(repo, "shared.txt", "trunk-side\n")

    result = h.vcs.update_to(ws, new_base)

    assert result.ok is False
    assert result.conflicts != []
    # Recovery: the workspace must be left clean, not mid-update.
    assert h.vcs.dirty_files(ws) == []


def test_update_to_recovers_from_interrupted_update(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "interrupted-branch", ws, base=base)
    h.commit(ws, "shared.txt", "workspace-side\n")
    new_base = h.conflicting_advance_trunk(repo, "shared.txt", "trunk-side\n")
    h.leave_interrupted_update(ws, new_base)

    result = h.vcs.update_to(ws, new_base)  # must self-recover, not raise

    assert result.ok is False
    assert h.vcs.dirty_files(ws) == []


def test_describe_changes_is_best_effort_string(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    base = h.vcs.resolve(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "describe-branch", ws, base=base)
    since = h.vcs.current_rev(ws)
    h.commit(ws, "new.txt", "new\n")

    out = h.vcs.describe_changes(ws, since=since)

    assert isinstance(out, str)


def test_submit_direct_lifecycle_reaches_landed(conformance_harness):
    h = conformance_harness
    repo = h.new_repo()
    # base is the TRUNK — the shape real callers pass (a ref-name-like string
    # for git; opaque for any other backend), not a resolved rev.
    base = h.vcs.trunk(repo)
    ws = h.new_workspace_path()
    h.vcs.create_workspace(repo, "submit-branch", ws, base=base)
    h.commit(ws, "feature.txt", "feat\n")

    result = h.vcs.submit(ws, base=base, mode="direct", push=False)

    assert result.status == "landed"
    assert result.landed_rev is not None
    assert h.vcs.resolve(repo) == result.landed_rev
    # The land-status seam: a git-direct land is synchronous, so the poller's
    # question is moot here — but the method must still answer, fail-closed.
    status = h.vcs.land_status(repo, result.landed_rev)
    assert status.state in ("landed", "unlanded", "failed")
