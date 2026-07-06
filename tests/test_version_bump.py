import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── classify_commit ────────────────────────────────────────────────────────

def test_classify_commit_feat_is_minor():
    from juggle_version_bump import classify_commit
    assert classify_commit("feat: add widget") == "minor"


def test_classify_commit_fix_is_patch():
    from juggle_version_bump import classify_commit
    assert classify_commit("fix: null deref in widget") == "patch"


def test_classify_commit_bang_is_major():
    from juggle_version_bump import classify_commit
    assert classify_commit("feat!: drop legacy API") == "major"


def test_classify_commit_breaking_change_footer_is_major():
    from juggle_version_bump import classify_commit
    msg = "feat: new config format\n\nBREAKING CHANGE: old format no longer parses"
    assert classify_commit(msg) == "major"


def test_classify_commit_scoped_prefix_still_classifies():
    from juggle_version_bump import classify_commit
    assert classify_commit("fix(cockpit): stale pane refresh") == "patch"


def test_classify_commit_unrecognized_prefix_returns_none():
    from juggle_version_bump import classify_commit
    assert classify_commit("chore: tidy imports") is None
    assert classify_commit("bump deps") is None
    assert classify_commit("") is None


# ── compute_bump ───────────────────────────────────────────────────────────

def test_compute_bump_picks_highest_rank():
    from juggle_version_bump import compute_bump
    assert compute_bump(["fix: a", "feat: b", "chore: c"]) == "minor"
    assert compute_bump(["fix: a", "feat!: b"]) == "major"


def test_compute_bump_none_when_nothing_qualifies():
    from juggle_version_bump import compute_bump
    assert compute_bump(["chore: a", "docs: b"]) is None
    assert compute_bump([]) is None


# ── bump_semver ────────────────────────────────────────────────────────────

def test_bump_semver_patch():
    from juggle_version_bump import bump_semver
    assert bump_semver("1.2.3", "patch") == "1.2.4"


def test_bump_semver_minor_resets_patch():
    from juggle_version_bump import bump_semver
    assert bump_semver("1.2.3", "minor") == "1.3.0"


def test_bump_semver_major_resets_minor_and_patch():
    from juggle_version_bump import bump_semver
    assert bump_semver("1.2.3", "major") == "2.0.0"


# ── apply_version_bump (real git repo) ──────────────────────────────────────

def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


def _init_repo_with_plugin(tmp_path, version="1.0.0"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    plugin_dir = repo / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "juggle", "version": version}))
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return str(repo)


def test_apply_version_bump_no_plugin_json_is_noop(tmp_path):
    from juggle_version_bump import apply_version_bump
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (repo / "b.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: add b")

    result = apply_version_bump(str(repo), since=base)
    assert result is None


def test_apply_version_bump_no_qualifying_commit_is_noop(tmp_path):
    from juggle_version_bump import apply_version_bump
    repo = _init_repo_with_plugin(tmp_path)
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (Path(repo) / "b.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "chore: tidy")

    result = apply_version_bump(repo, since=base)
    assert result is None
    head_after = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    head_before = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD~0"],
                                  capture_output=True, text=True).stdout.strip()
    assert head_after == head_before  # no bump commit added


def test_apply_version_bump_feat_commits_and_writes_version(tmp_path):
    from juggle_version_bump import apply_version_bump
    repo = _init_repo_with_plugin(tmp_path, version="1.2.3")
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    (Path(repo) / "b.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: add b")

    result = apply_version_bump(repo, since=base)

    assert result == "1.3.0"
    data = json.loads((Path(repo) / ".claude-plugin" / "plugin.json").read_text())
    assert data["version"] == "1.3.0"
    log = subprocess.run(["git", "-C", repo, "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout
    assert "bump to 1.3.0" in log


def test_apply_version_bump_earlier_fix_bumps_when_newest_is_nonbumping(tmp_path):
    """Regression pin — 2026-07-05: operator integrate NE landed 4 fix: commits
    without bumping version. Root cause: _commit_messages split on the %x00
    record terminator, but `git log` inserts a `\\n` BETWEEN records, so every
    segment after the newest carried a leading blank line and classify_commit's
    first-line lookup saw '' → None. The bump thus reflected ONLY the newest
    commit in the range; when that newest commit was a non-bumping one
    (test:/refactor:) the whole landing silently no-op'd. This pins the
    multi-commit range where the NEWEST commit does not warrant a bump but an
    earlier fix: does — the patch bump MUST still fire."""
    from juggle_version_bump import apply_version_bump
    repo = _init_repo_with_plugin(tmp_path, version="1.120.0")
    base = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    # Mirror NE's topology: a fix: commit, then a refactor:, then a test: TIP.
    (Path(repo) / "fix.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix(spool): D3 — supersede success-sign set")
    (Path(repo) / "refactor.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "refactor(spool): extract journal helpers")
    (Path(repo) / "test.py").write_text("z = 3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "test(spool): drop unused imports")

    result = apply_version_bump(repo, since=base)

    assert result == "1.120.1"
    data = json.loads((Path(repo) / ".claude-plugin" / "plugin.json").read_text())
    assert data["version"] == "1.120.1"
    log = subprocess.run(["git", "-C", repo, "log", "--oneline", "-1"],
                          capture_output=True, text=True).stdout
    assert "bump to 1.120.1" in log


def test_apply_version_bump_reads_current_version_at_since_not_stale(tmp_path):
    """The version comes from the repo's checked-out plugin.json at call time
    (already rebased onto latest main), not from any cached/prior value —
    guards the 'two parallel branches land cleanly with two bumps' pin."""
    from juggle_version_bump import apply_version_bump
    repo = _init_repo_with_plugin(tmp_path, version="1.0.0")
    base1 = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (Path(repo) / "a.py").write_text("y = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat: first")
    v1 = apply_version_bump(repo, since=base1)
    assert v1 == "1.1.0"

    base2 = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    (Path(repo) / "b.py").write_text("z = 3\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fix: second")
    v2 = apply_version_bump(repo, since=base2)
    assert v2 == "1.1.1"
