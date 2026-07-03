"""TDD pins for PromptContext dataclasses, VCS-verb resolution, and repo
profile resolution (Agent Prompt Contract v2, PC1 —
plan/2026-07-03-agent-prompt-contract-v2.md)."""
from __future__ import annotations

import json
from pathlib import Path

from juggle_prompt_context import UNBOUND_VCS, vcs_info_for
from juggle_repo_profile import is_juggle_repo, resolve_repo_profile

REPO_ROOT = Path(__file__).parent.parent


def test_vcs_info_for_git_repo_resolves_git_verbs():
    info = vcs_info_for(str(REPO_ROOT))
    assert info.name == "git"
    assert info.commit_verb == "git commit"
    assert info.workspace_noun == "branch"


def test_vcs_info_for_undetectable_repo_falls_back_to_generic(tmp_path):
    info = vcs_info_for(str(tmp_path))
    assert info == UNBOUND_VCS
    assert "git" not in info.commit_verb.lower()


def test_is_juggle_repo_true_for_this_repo():
    assert is_juggle_repo(str(REPO_ROOT)) is True


def test_is_juggle_repo_false_for_foreign_repo(tmp_path):
    assert is_juggle_repo(str(tmp_path)) is False


def test_resolve_repo_profile_juggle_repo_folds_doctor_smoke_into_verify():
    profile = resolve_repo_profile(str(REPO_ROOT))
    assert profile.full_suite_cmd == "juggle verify"
    assert profile.quality_gate_skill == "mike:pre-pr"
    assert profile.version_bump_policy


def test_resolve_repo_profile_foreign_repo_defaults_to_none(tmp_path):
    profile = resolve_repo_profile(str(tmp_path))
    assert profile.full_suite_cmd is None
    assert profile.smoke_cmd is None
    assert profile.quality_gate_skill is None
    assert profile.version_bump_policy is None


def test_resolve_repo_profile_reads_claude_md_juggle_section(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Project\n\nsome unrelated text\n\n"
        "# juggle\n"
        "full_suite_cmd: make ci\n"
        "quality_gate_skill: acme:pre-pr\n"
        "\n"
        "# Another Section\n"
        "full_suite_cmd: ignored\n"
    )
    profile = resolve_repo_profile(str(tmp_path))
    assert profile.full_suite_cmd == "make ci"
    assert profile.quality_gate_skill == "acme:pre-pr"


def test_resolve_repo_profile_db_config_overrides_generic_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg_path.write_text(json.dumps({"repos": {str(repo_dir): {"test_cmd": "make test"}}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg_path))
    profile = resolve_repo_profile(str(repo_dir))
    assert profile.full_suite_cmd == "make test"


def test_resolve_repo_profile_claude_md_overrides_db_config(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    cfg_path.write_text(json.dumps({"repos": {str(repo_dir): {"test_cmd": "make test"}}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg_path))
    (repo_dir / "CLAUDE.md").write_text("# juggle\nfull_suite_cmd: make ci\n")
    profile = resolve_repo_profile(str(repo_dir))
    assert profile.full_suite_cmd == "make ci"
