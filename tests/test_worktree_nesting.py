"""Regression: nested worktree creation must not compound the path basename."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from juggle_cmd_agents_worktree import _create_worktree, _main_worktree_root


def _init_repo(root: Path) -> Path:
    repo = root / "juggle"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_nested_create_does_not_compound_basename(tmp_path):
    repo = _init_repo(tmp_path)
    wt_root = str(tmp_path / "wts")
    Path(wt_root).mkdir()

    ok, path1, branch1, _ = _create_worktree(str(repo), "WR", worktree_root=wt_root)
    assert ok, _
    assert Path(path1).name == "juggle-juggle-WR", path1

    # Now create a SECOND worktree while passing the FIRST worktree as repo_path
    # (simulates an agent dispatching from inside its own worktree). The basename
    # must stay "juggle", never compound to "juggle-juggle-WR-...".
    ok2, path2, branch2, msg2 = _create_worktree(path1, "XX", worktree_root=wt_root)
    assert ok2, msg2
    # Normal nesting is "juggle-juggle-XX" (template prefixes a literal
    # "juggle-" + repo basename "juggle"). The BUG is triple+ compounding.
    assert Path(path2).name == "juggle-juggle-XX", f"compounded: {path2}"
    assert "juggle-juggle-juggle" not in Path(path2).name


def test_main_worktree_root_resolves_primary(tmp_path):
    repo = _init_repo(tmp_path)
    wt_root = str(tmp_path / "wts")
    Path(wt_root).mkdir()
    ok, path1, _, _ = _create_worktree(str(repo), "WR", worktree_root=wt_root)
    assert ok
    # Resolving from inside the linked worktree must point back to the primary.
    assert Path(_main_worktree_root(path1)).resolve() == repo.resolve()
