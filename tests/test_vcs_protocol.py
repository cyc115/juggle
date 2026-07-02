"""Tests for the widened VCS Protocol (SPEC 2026-07-02 §1-2): the 15
intent-level methods, result dataclasses, GitVCS, and backend_for().

Topic vcs-seam / task vcs-protocol-v1. Behavior-preserving extraction: GitVCS
method bodies must reproduce the exact git recipes currently inline in
juggle_cmd_integrate.py / juggle_cmd_agents_worktree.py / dbops/graph_guards.py
(facts §A) — call-site rewiring is a LATER topic, this only pins the seam.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402
from vcs_types import (  # noqa: E402
    Capabilities,
    LandStatus,
    SubmitResult,
    UpdateResult,
    WorkspaceResult,
)

from helpers.fake_vcs import FakeBackend  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


def _commit(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def origin(tmp_path):
    o = tmp_path / "origin.git"
    o.mkdir()
    _git(o, "init", "-q", "--bare", "-b", "main")
    return o


@pytest.fixture
def main_repo(tmp_path, origin):
    repo = tmp_path / "main"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _commit(repo, "a.txt", "one\n")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


@pytest.fixture
def ws(tmp_path, main_repo):
    """A workspace branched off main_repo's HEAD via GitVCS.create_workspace."""
    g = vcs_mod.GitVCS()
    root = str(tmp_path / "ws1")
    base = g.resolve(str(main_repo))
    result = g.create_workspace(str(main_repo), "cyc_topic1", root, base=base)
    assert result.ok
    return Path(root)


# ---------------------------------------------------------------------------
# VCS_PROTOCOL_VERSION + dataclasses
# ---------------------------------------------------------------------------


def test_protocol_version_is_1():
    assert vcs_mod.VCS_PROTOCOL_VERSION == 1


def test_result_dataclasses_are_frozen():
    ws_result = WorkspaceResult(ok=True, path="/p", branch="b")
    with pytest.raises(Exception):
        ws_result.ok = False  # type: ignore[misc]

    upd = UpdateResult(ok=False, conflicts=["a.txt"], detail="conflict")
    assert upd.conflicts == ["a.txt"]

    sub = SubmitResult(status="landed", landed_rev="abc123")
    assert sub.status == "landed" and sub.landed_rev == "abc123"

    land = LandStatus(state="unlanded")
    assert land.state == "unlanded"

    caps = Capabilities(async_land=True, auto_restack=False)
    assert caps.async_land is True


# ---------------------------------------------------------------------------
# GitVCS — identity & detection
# ---------------------------------------------------------------------------


def test_repo_root_resolves_toplevel(main_repo):
    g = vcs_mod.GitVCS()
    assert g.repo_root(str(main_repo)) == str(main_repo.resolve())


def test_repo_root_none_for_non_repo(tmp_path):
    assert vcs_mod.GitVCS().repo_root(str(tmp_path)) is None


def test_primary_root_from_worktree(main_repo, ws):
    g = vcs_mod.GitVCS()
    assert g.primary_root(str(ws)) == str(main_repo.resolve())


# ---------------------------------------------------------------------------
# GitVCS — history facts
# ---------------------------------------------------------------------------


def test_trunk_resolves_local_main(main_repo):
    g = vcs_mod.GitVCS()
    assert g.trunk(str(main_repo)) in ("origin/main", "main")


def test_trunk_none_when_no_main_or_master(tmp_path):
    repo = tmp_path / "nomain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "weird")
    _commit(repo, "a.txt", "x\n")
    assert vcs_mod.GitVCS().trunk(str(repo)) is None


def test_resolve_none_ref_returns_head(main_repo):
    g = vcs_mod.GitVCS()
    expected = _git(main_repo, "rev-parse", "HEAD").stdout.strip()
    assert g.resolve(str(main_repo)) == expected


def test_resolve_named_ref(main_repo):
    g = vcs_mod.GitVCS()
    assert g.resolve(str(main_repo), "main") == g.resolve(str(main_repo))


def test_resolve_unknown_ref_is_none(main_repo):
    assert vcs_mod.GitVCS().resolve(str(main_repo), "no-such-ref") is None


def test_is_ancestor_true_for_own_head(main_repo):
    g = vcs_mod.GitVCS()
    head = g.resolve(str(main_repo))
    assert g.is_ancestor(str(main_repo), head, "main") is True


def test_is_ancestor_false_closed_on_bad_repo(tmp_path):
    assert vcs_mod.GitVCS().is_ancestor(str(tmp_path / "nope"), "deadbeef", "main") is False


def test_is_ancestor_false_for_nonancestor(main_repo, ws):
    g = vcs_mod.GitVCS()
    _commit(ws, "feature.txt", "feat\n")
    feature_head = g.current_rev(str(ws))
    assert g.is_ancestor(str(main_repo), feature_head, "main") is False


# ---------------------------------------------------------------------------
# GitVCS — workspace lifecycle
# ---------------------------------------------------------------------------


def test_create_workspace_forks_at_explicit_base(tmp_path, main_repo):
    g = vcs_mod.GitVCS()
    base = g.resolve(str(main_repo))
    root = str(tmp_path / "ws2")
    result = g.create_workspace(str(main_repo), "cyc_x", root, base=base)
    assert result.ok is True
    assert result.path == root
    assert result.branch == "cyc_x"
    assert g.current_rev(root) == base


def test_create_workspace_idempotent_if_exists(ws, main_repo):
    g = vcs_mod.GitVCS()
    result = g.create_workspace(str(main_repo), "cyc_topic1", str(ws), base=None)
    assert result.ok is True
    assert "already exists" in result.detail


def test_remove_workspace_removes_dir_and_branch(main_repo, ws):
    g = vcs_mod.GitVCS()
    assert ws.exists()
    ok = g.remove_workspace(str(main_repo), str(ws))
    assert ok is True
    assert not ws.exists()
    branches = _git(main_repo, "branch", "--list", "cyc_topic1").stdout
    assert "cyc_topic1" not in branches


def test_current_rev_matches_head(ws):
    g = vcs_mod.GitVCS()
    expected = _git(ws, "rev-parse", "HEAD").stdout.strip()
    assert g.current_rev(str(ws)) == expected


def test_dirty_files_empty_then_populated(ws):
    g = vcs_mod.GitVCS()
    assert g.dirty_files(str(ws)) == []
    (ws / "scratch.txt").write_text("wip\n")
    assert g.dirty_files(str(ws)) != []


def test_has_changes_false_then_true(ws):
    g = vcs_mod.GitVCS()
    since = g.current_rev(str(ws))
    assert g.has_changes(str(ws), since=since) is False
    _commit(ws, "new.txt", "new\n")
    assert g.has_changes(str(ws), since=since) is True


def test_update_to_rebases_onto_new_base(main_repo, ws):
    g = vcs_mod.GitVCS()
    _commit(ws, "feature.txt", "feat\n")
    _commit(main_repo, "b.txt", "two\n")
    new_base = g.resolve(str(main_repo))
    result = g.update_to(str(ws), new_base)
    assert result.ok is True
    assert result.conflicts == []
    assert g.is_ancestor(str(ws), new_base, "HEAD") is True


def test_update_to_conflict_reports_and_aborts(main_repo, ws):
    g = vcs_mod.GitVCS()
    (ws / "a.txt").write_text("ws-change\n")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "conflict on ws side")
    (main_repo / "a.txt").write_text("main-change\n")
    _git(main_repo, "add", "a.txt")
    _git(main_repo, "commit", "-q", "-m", "conflict on main side")
    new_base = g.resolve(str(main_repo))

    result = g.update_to(str(ws), new_base)

    assert result.ok is False
    assert "a.txt" in result.conflicts
    # aborted cleanly — no rebase left in progress
    status = _git(ws, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_update_to_recovers_from_interrupted_rebase(main_repo, ws):
    g = vcs_mod.GitVCS()
    (ws / "a.txt").write_text("ws-change\n")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "conflict on ws side")
    (main_repo / "a.txt").write_text("main-change\n")
    _git(main_repo, "add", "a.txt")
    _git(main_repo, "commit", "-q", "-m", "conflict on main side")
    new_base = g.resolve(str(main_repo))
    _git(ws, "rebase", new_base, check=False)  # leave interrupted, no abort

    result = g.update_to(str(ws), new_base)  # must self-recover, not crash

    assert result.ok is False
    status = _git(ws, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_describe_changes_returns_diffstat(main_repo, ws):
    g = vcs_mod.GitVCS()
    since = g.current_rev(str(ws))
    _commit(ws, "new.txt", "new\n")
    out = g.describe_changes(str(ws), since=since)
    assert "new.txt" in out


# ---------------------------------------------------------------------------
# GitVCS — publish
# ---------------------------------------------------------------------------


def test_submit_direct_ff_merges_and_pushes(main_repo, origin, ws):
    g = vcs_mod.GitVCS()
    _commit(ws, "feature.txt", "feat\n")
    base = g.resolve(str(main_repo))

    result = g.submit(str(ws), base=base, mode="direct")

    assert result.status == "landed"
    assert result.landed_rev == g.resolve(str(main_repo))
    assert (main_repo / "feature.txt").exists()
    origin_head = subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert origin_head == result.landed_rev


def test_submit_queue_pushes_branch_only(main_repo, origin, ws):
    g = vcs_mod.GitVCS()
    _commit(ws, "feature.txt", "feat\n")
    base = g.resolve(str(main_repo))

    result = g.submit(str(ws), base=base, mode="queue")

    assert result.status == "submitted"
    assert result.ticket == "cyc_topic1"
    # local main untouched
    assert not (main_repo / "feature.txt").exists()
    remote_branches = subprocess.run(
        ["git", "--git-dir", str(origin), "branch", "--list", "cyc_topic1"],
        capture_output=True, text=True,
    ).stdout
    assert "cyc_topic1" in remote_branches


def test_land_status_defaults_to_pending(main_repo):
    g = vcs_mod.GitVCS()
    status = g.land_status(str(main_repo), "some-ticket")
    assert status.state == "unlanded"


# ---------------------------------------------------------------------------
# back-compat aliases
# ---------------------------------------------------------------------------


def test_head_alias_matches_resolve(main_repo):
    g = vcs_mod.GitVCS()
    assert g.head(str(main_repo)) == g.resolve(str(main_repo))


def test_is_dirty_alias_matches_dirty_files(ws):
    g = vcs_mod.GitVCS()
    assert g.is_dirty(str(ws)) is False
    (ws / "x.txt").write_text("y\n")
    assert g.is_dirty(str(ws)) is True


# ---------------------------------------------------------------------------
# backend_for()
# ---------------------------------------------------------------------------


def test_backend_for_autodetects_git(main_repo, monkeypatch):
    vcs_mod._BACKEND_CACHE.clear()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": None}
    )
    backend = vcs_mod.backend_for(str(main_repo))
    assert backend.name == "git"


def test_backend_for_caches_per_primary_root(main_repo, ws, monkeypatch):
    vcs_mod._BACKEND_CACHE.clear()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": None}
    )
    b1 = vcs_mod.backend_for(str(main_repo))
    b2 = vcs_mod.backend_for(str(ws))  # a worktree of the SAME repo
    assert b1 is b2


def test_backend_for_raises_on_unknown_override(main_repo, monkeypatch):
    vcs_mod._BACKEND_CACHE.clear()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": "sapling"}
    )
    with pytest.raises(ValueError):
        vcs_mod.backend_for(str(main_repo))


def test_backend_for_raises_on_undetectable_repo(tmp_path, monkeypatch):
    vcs_mod._BACKEND_CACHE.clear()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": None}
    )
    with pytest.raises(ValueError):
        vcs_mod.backend_for(str(tmp_path))


# ---------------------------------------------------------------------------
# FakeBackend — records calls, scripts results (SPEC §6 test strategy)
# ---------------------------------------------------------------------------


def test_fake_backend_records_calls():
    fb = FakeBackend()
    fb.trunk("/repo")
    fb.is_ancestor("/repo", "sha1", "main")
    names = [c[0] for c in fb.calls]
    assert names == ["trunk", "is_ancestor"]


def test_fake_backend_scripted_result():
    fb = FakeBackend()
    fb.scripted["submit"] = SubmitResult(status="submitted", ticket="T-1")
    result = fb.submit("/ws", base="base-rev", mode="queue")
    assert result.status == "submitted"
    assert result.ticket == "T-1"


def test_fake_backend_satisfies_vcs_protocol():
    assert isinstance(FakeBackend(), vcs_mod.VCS)
