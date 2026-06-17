"""
Tests for scripts/git-merge-plugin-version.py merge driver.
2026-06-17: auto-resolve plugin.json version conflicts by taking max semver.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

DRIVER = Path(__file__).parent.parent / "scripts" / "git-merge-plugin-version.py"


def _run_driver(base: dict, ours: dict, theirs: dict) -> tuple[int, dict | None]:
    """Run the merge driver with three temp files; return (exit_code, merged_json_or_None)."""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        base_f = d / "base.json"
        ours_f = d / "ours.json"
        theirs_f = d / "theirs.json"
        base_f.write_text(json.dumps(base))
        ours_f.write_text(json.dumps(ours))
        theirs_f.write_text(json.dumps(theirs))
        result = subprocess.run(
            [sys.executable, str(DRIVER), str(base_f), str(ours_f), str(theirs_f)],
            capture_output=True,
            text=True,
        )
        merged = None
        if result.returncode == 0:
            merged = json.loads(ours_f.read_text())
        return result.returncode, merged


# ---------------------------------------------------------------------------
# Unit tests: driver logic
# ---------------------------------------------------------------------------

def test_unit_max_version_ours_wins():
    """When ours version > theirs, result keeps ours version."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.2.0", "name": "juggle"}
    theirs = {"version": "1.1.0", "name": "juggle"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged["version"] == "1.2.0"


def test_unit_max_version_theirs_wins():
    """When theirs version > ours, result takes theirs version."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.1.0", "name": "juggle"}
    theirs = {"version": "1.2.0", "name": "juggle"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged["version"] == "1.2.0"


def test_unit_max_version_equal():
    """When ours and theirs have identical versions, exit 0 with that version."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.1.0", "name": "juggle"}
    theirs = {"version": "1.1.0", "name": "juggle"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged["version"] == "1.1.0"


def test_unit_semver_major_beats_minor():
    """Semver comparison: 2.0.0 > 1.99.99."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.99.99", "name": "juggle"}
    theirs = {"version": "2.0.0", "name": "juggle"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged["version"] == "2.0.0"


def test_unit_non_version_conflict_exits_1():
    """When a non-version field differs between ours and theirs, exit 1 (human must resolve)."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.1.0", "name": "juggle-ours"}
    theirs = {"version": "1.2.0", "name": "juggle-theirs"}
    rc, _ = _run_driver(base, ours, theirs)
    assert rc == 1


def test_unit_only_version_differs_no_conflict():
    """Non-version fields same in ours/theirs → exit 0, only version changes."""
    base = {"version": "1.0.0", "name": "juggle", "author": "mike"}
    ours = {"version": "1.1.0", "name": "juggle", "author": "mike"}
    theirs = {"version": "1.3.0", "name": "juggle", "author": "mike"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged["version"] == "1.3.0"
    assert merged["name"] == "juggle"
    assert merged["author"] == "mike"


def test_unit_result_is_based_on_ours():
    """Result file is ours with version updated — other fields come from ours, not theirs."""
    base = {"version": "1.0.0", "name": "juggle"}
    ours = {"version": "1.1.0", "name": "juggle", "extra": "ours-value"}
    theirs = {"version": "1.2.0", "name": "juggle"}
    rc, merged = _run_driver(base, ours, theirs)
    assert rc == 0
    assert merged.get("extra") == "ours-value"


# ---------------------------------------------------------------------------
# Integration test: real git repo with rebase
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path, env=None) -> str:
    full_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, env=full_env
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


@pytest.fixture()
def git_repo(tmp_path):
    """Create a bare git repo with the merge driver configured."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@test.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)

    # Configure the juggle-version driver
    driver_cmd = f"{sys.executable} {DRIVER} %O %A %B"
    _git(["config", "merge.juggle-version.driver", driver_cmd], cwd=repo)

    # .gitattributes
    (repo / ".gitattributes").write_text(".claude-plugin/plugin.json merge=juggle-version\n")

    # Initial plugin.json
    plugin_dir = repo / ".claude-plugin"
    plugin_dir.mkdir()
    base_json = {"version": "1.0.0", "name": "juggle"}
    (plugin_dir / "plugin.json").write_text(json.dumps(base_json, indent=2))

    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    return repo


def test_integration_rebase_auto_resolves_version_conflict(git_repo):
    """Rebase of two branches each bumping version auto-resolves to max version."""
    repo = git_repo

    # Branch A bumps to 1.1.0
    _git(["checkout", "-b", "branch-a"], cwd=repo)
    p = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    p["version"] = "1.1.0"
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps(p, indent=2))
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "bump 1.1.0"], cwd=repo)

    # Branch B (from main) bumps to 1.2.0
    _git(["checkout", "main"], cwd=repo)
    _git(["checkout", "-b", "branch-b"], cwd=repo)
    p = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    p["version"] = "1.2.0"
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps(p, indent=2))
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "bump 1.2.0"], cwd=repo)

    # Merge branch-a into branch-b — should auto-resolve
    _git(["merge", "branch-a", "--no-edit"], cwd=repo)

    merged = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    assert merged["version"] == "1.2.0"


def test_integration_non_version_conflict_surfaces(git_repo):
    """Non-version conflict is NOT auto-resolved — merge exits with conflict."""
    repo = git_repo

    # Branch A changes name
    _git(["checkout", "-b", "branch-a"], cwd=repo)
    p = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    p["version"] = "1.1.0"
    p["name"] = "juggle-fork"
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps(p, indent=2))
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "rename"], cwd=repo)

    # Branch B (from main) changes name differently + bumps version
    _git(["checkout", "main"], cwd=repo)
    _git(["checkout", "-b", "branch-b"], cwd=repo)
    p = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    p["version"] = "1.2.0"
    p["name"] = "juggle-renamed"
    (repo / ".claude-plugin" / "plugin.json").write_text(json.dumps(p, indent=2))
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "bump+rename"], cwd=repo)

    # Merge should FAIL with conflict
    result = subprocess.run(
        ["git", "merge", "branch-a", "--no-edit"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, "Expected merge to fail on non-version conflict"
    # Clean up conflict state
    subprocess.run(["git", "merge", "--abort"], cwd=repo, capture_output=True)
