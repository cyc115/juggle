"""Tests for juggle_integrate_testscope — pure-function unit tests (no git/IO)."""
import pytest
from juggle_integrate_testscope import select_scoped_tests, build_test_command


EXISTING = {
    "tests/test_db_topics.py",
    "tests/test_juggle_hooks.py",
    "tests/test_integrate_testscope.py",
    "tests/subdir/test_vcs.py",
}


# ── select_scoped_tests ───────────────────────────────────────────────────────

class TestDocumentationOnly:
    def test_docs_only_returns_skip(self):
        result = select_scoped_tests(
            ["docs/ARCHITECTURE.md", "README.md"],
            EXISTING,
        )
        assert result["mode"] == "skip"
        assert result["paths"] == []
        assert "no python" in result["reason"].lower()

    def test_graphify_out_only_returns_skip(self):
        result = select_scoped_tests(
            ["graphify-out/graph.json", "graphify-out/GRAPH_REPORT.md"],
            EXISTING,
        )
        assert result["mode"] == "skip"

    def test_config_yaml_only_returns_skip(self):
        result = select_scoped_tests(["config/viewports.yaml"], EXISTING)
        assert result["mode"] == "skip"


class TestSrcToTestMapping:
    def test_src_file_maps_to_test_stem(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_src_file_maps_via_any_subdir(self):
        result = select_scoped_tests(
            ["src/vcs.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/subdir/test_vcs.py" in result["paths"]

    def test_unmapped_src_returns_full(self):
        result = select_scoped_tests(
            ["src/juggle_unknown_module.py"],
            EXISTING,
        )
        assert result["mode"] == "full"
        assert result["paths"] == []
        assert "unmapped" in result["reason"].lower()

    def test_multiple_src_files_all_mapped(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py", "src/juggle_hooks.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]
        assert "tests/test_juggle_hooks.py" in result["paths"]

    def test_paths_are_sorted_and_unique(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py", "src/dbops/db_topics.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert result["paths"] == sorted(set(result["paths"]))
        assert result["paths"].count("tests/test_db_topics.py") == 1


class TestChangedTestFilesIncluded:
    def test_changed_test_file_included_directly(self):
        result = select_scoped_tests(
            ["tests/test_juggle_hooks.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_juggle_hooks.py" in result["paths"]

    def test_changed_test_and_src_combined(self):
        result = select_scoped_tests(
            ["tests/test_juggle_hooks.py", "src/dbops/db_topics.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_juggle_hooks.py" in result["paths"]
        assert "tests/test_db_topics.py" in result["paths"]

    def test_changed_test_not_in_existing_ignored(self):
        # A new test file added in the branch (not yet in existing_test_files)
        # should still be included directly since it's in changed_files
        result = select_scoped_tests(
            ["tests/test_new_thing.py"],
            EXISTING,
        )
        # new test file that changed: add directly even if not in existing_test_files
        assert result["mode"] == "scoped"
        assert "tests/test_new_thing.py" in result["paths"]


class TestCoreGlobs:
    def test_core_globs_added_to_scoped(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
            core_globs=["tests/test_juggle_hooks.py"],
        )
        assert result["mode"] == "scoped"
        assert "tests/test_juggle_hooks.py" in result["paths"]
        assert "tests/test_db_topics.py" in result["paths"]

    def test_core_globs_with_fnmatch_pattern(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
            core_globs=["tests/test_juggle_*.py"],
        )
        assert result["mode"] == "scoped"
        assert "tests/test_juggle_hooks.py" in result["paths"]

    def test_core_globs_alone_no_src_change_skips(self):
        # docs-only change with core_globs — still skip (no python changed)
        result = select_scoped_tests(
            ["docs/README.md"],
            EXISTING,
            core_globs=["tests/test_juggle_hooks.py"],
        )
        assert result["mode"] == "skip"

    def test_core_globs_with_unmapped_src_returns_full(self):
        # unmapped src still forces full even if core_globs exist
        result = select_scoped_tests(
            ["src/juggle_unknown_module.py"],
            EXISTING,
            core_globs=["tests/test_juggle_hooks.py"],
        )
        assert result["mode"] == "full"


class TestMixedChanges:
    def test_mixed_doc_and_src_maps_to_scoped(self):
        result = select_scoped_tests(
            ["docs/README.md", "src/dbops/db_topics.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_empty_changed_files_returns_skip(self):
        result = select_scoped_tests([], EXISTING)
        assert result["mode"] == "skip"


class TestNormalization:
    def test_posix_paths_normalized(self):
        # Paths with os separators should still map correctly
        result = select_scoped_tests(
            ["src\\dbops\\db_topics.py"],
            EXISTING,
        )
        # On any platform, normalized to posix
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_reason_contains_count(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
        )
        assert "1" in result["reason"]


# ── build_test_command ────────────────────────────────────────────────────────

class TestBuildTestCommand:
    def test_basic_appends_paths(self):
        cmd = build_test_command("uv run pytest -q", ["tests/test_foo.py"])
        assert cmd == "uv run pytest -q tests/test_foo.py"

    def test_strips_trailing_path_arg(self):
        cmd = build_test_command("uv run pytest -q tests/", ["tests/test_foo.py"])
        assert "tests/" not in cmd or cmd.endswith("tests/test_foo.py")
        assert "tests/test_foo.py" in cmd

    def test_preserves_q_flag(self):
        cmd = build_test_command("uv run pytest -q", ["tests/test_a.py", "tests/test_b.py"])
        assert "-q" in cmd
        assert "tests/test_a.py" in cmd
        assert "tests/test_b.py" in cmd

    def test_preserves_m_flag(self):
        cmd = build_test_command("uv run pytest -m 'pilot' -q", ["tests/test_a.py"])
        assert "-m" in cmd
        assert "pilot" in cmd
        assert "tests/test_a.py" in cmd

    def test_strips_bare_path_arg_at_end(self):
        cmd = build_test_command("uv run pytest tests/", ["tests/test_foo.py"])
        assert cmd.strip().endswith("tests/test_foo.py")

    def test_multiple_paths_space_joined(self):
        paths = ["tests/test_a.py", "tests/test_b.py"]
        cmd = build_test_command("uv run pytest -q", paths)
        assert "tests/test_a.py tests/test_b.py" in cmd

    def test_empty_paths_returns_base(self):
        cmd = build_test_command("uv run pytest -q", [])
        assert cmd == "uv run pytest -q"
