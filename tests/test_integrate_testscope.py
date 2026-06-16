"""Tests for juggle_integrate_testscope — pure-function unit tests (no git/IO)."""
import textwrap
from pathlib import Path

import pytest
from juggle_integrate_testscope import (
    apply_quarantine,
    build_import_index,
    build_test_command,
    select_scoped_tests,
)


EXISTING = {
    "tests/test_db_topics.py",
    "tests/test_juggle_hooks.py",
    "tests/test_integrate_testscope.py",
    "tests/subdir/test_vcs.py",
}


# ── select_scoped_tests (name-stem only, no import index) ─────────────────────

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

    def test_changed_test_not_in_existing_included_directly(self):
        # New test file added in branch (not yet in existing_test_files).
        result = select_scoped_tests(
            ["tests/test_new_thing.py"],
            EXISTING,
        )
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
        result = select_scoped_tests(
            ["docs/README.md"],
            EXISTING,
            core_globs=["tests/test_juggle_hooks.py"],
        )
        assert result["mode"] == "skip"

    def test_core_globs_with_unmapped_src_returns_full(self):
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
        result = select_scoped_tests(
            ["src\\dbops\\db_topics.py"],
            EXISTING,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_reason_contains_count(self):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
        )
        assert "1" in result["reason"]


# ── Import-reference mapping (Fix 1) ─────────────────────────────────────────

class TestImportReferenceMapping:
    """select_scoped_tests uses import_index as primary mapping strategy."""

    COCKPIT_EXISTING = {
        "tests/test_cockpit_task_detail.py",
        "tests/test_juggle_smoke.py",
        "tests/test_cockpit_splitter_resize.py",
    }
    COCKPIT_INDEX = {
        "juggle_cockpit": {
            "tests/test_cockpit_task_detail.py",
            "tests/test_juggle_smoke.py",
            "tests/test_cockpit_splitter_resize.py",
        }
    }

    def test_cockpit_maps_to_importers_via_index(self):
        result = select_scoped_tests(
            ["src/juggle_cockpit.py"],
            self.COCKPIT_EXISTING,
            import_index=self.COCKPIT_INDEX,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_cockpit_task_detail.py" in result["paths"]

    def test_import_index_takes_priority_over_name_stem(self):
        # juggle_cockpit has no test_juggle_cockpit.py, only importers
        result = select_scoped_tests(
            ["src/juggle_cockpit.py"],
            self.COCKPIT_EXISTING,
            import_index=self.COCKPIT_INDEX,
        )
        assert result["mode"] == "scoped"
        # must include all importers
        assert "tests/test_juggle_smoke.py" in result["paths"]
        assert "tests/test_cockpit_splitter_resize.py" in result["paths"]

    def test_name_stem_used_when_not_in_index(self):
        # db_topics has test_db_topics.py by name — index doesn't cover it
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
            import_index={},  # empty index
        )
        # falls back to name-stem mapping
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_unmapped_in_both_index_and_name_returns_full(self):
        result = select_scoped_tests(
            ["src/juggle_cockpit.py"],
            {"tests/test_something_else.py"},
            import_index={},  # not in index, no name match
        )
        assert result["mode"] == "full"

    def test_index_none_behaviour_unchanged(self):
        # No index provided — old name-stem behaviour
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            EXISTING,
            import_index=None,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_db_topics.py" in result["paths"]

    def test_pkg_module_stem_matched(self):
        # src/dbops/threads.py stem is "threads"; index key "threads"
        index = {"threads": {"tests/test_thread_dedup.py", "tests/test_migrations_label_retire.py"}}
        existing = {"tests/test_thread_dedup.py", "tests/test_migrations_label_retire.py"}
        result = select_scoped_tests(
            ["src/dbops/threads.py"],
            existing,
            import_index=index,
        )
        assert result["mode"] == "scoped"
        assert "tests/test_thread_dedup.py" in result["paths"]
        assert "tests/test_migrations_label_retire.py" in result["paths"]


# ── build_import_index (Fix 1 — impure helper) ───────────────────────────────

class TestBuildImportIndex:
    def _write(self, tmp_path, name, content):
        p = tmp_path / name
        p.write_text(textwrap.dedent(content))
        return str(p.relative_to(tmp_path))

    def test_detects_direct_import(self, tmp_path):
        rel = self._write(tmp_path, "test_foo.py", """\
            import juggle_cockpit
            def test_x(): pass
        """)
        idx = build_import_index(tmp_path)
        assert "juggle_cockpit" in idx
        assert any("test_foo.py" in p for p in idx["juggle_cockpit"])

    def test_detects_from_import(self, tmp_path):
        rel = self._write(tmp_path, "test_bar.py", """\
            from juggle_cockpit import CockpitApp
            def test_x(): pass
        """)
        idx = build_import_index(tmp_path)
        assert "juggle_cockpit" in idx
        assert any("test_bar.py" in p for p in idx["juggle_cockpit"])

    def test_detects_pkg_dot_stem(self, tmp_path):
        # "from dbops.threads import foo" → key "threads"
        self._write(tmp_path, "test_threads.py", """\
            from dbops.threads import create_thread
            def test_x(): pass
        """)
        idx = build_import_index(tmp_path)
        assert "threads" in idx
        assert any("test_threads.py" in p for p in idx["threads"])

    def test_detects_import_pkg_dot_mod(self, tmp_path):
        # "import dbops.threads as t" → key "threads"
        self._write(tmp_path, "test_t.py", """\
            import dbops.threads as _threads
            def test_x(): pass
        """)
        idx = build_import_index(tmp_path)
        assert "threads" in idx

    def test_ignores_non_test_files(self, tmp_path):
        (tmp_path / "helper.py").write_text("import juggle_cockpit\n")
        idx = build_import_index(tmp_path)
        assert "juggle_cockpit" not in idx

    def test_multiple_test_files_same_module(self, tmp_path):
        self._write(tmp_path, "test_a.py", "from juggle_cockpit import CockpitApp\n")
        self._write(tmp_path, "test_b.py", "import juggle_cockpit\n")
        idx = build_import_index(tmp_path)
        paths = idx["juggle_cockpit"]
        assert len(paths) == 2

    def test_returns_repo_relative_paths(self, tmp_path):
        self._write(tmp_path, "test_a.py", "import juggle_cockpit\n")
        idx = build_import_index(tmp_path)
        for p in idx["juggle_cockpit"]:
            assert not Path(p).is_absolute()


# ── Real-repo regression assertions ──────────────────────────────────────────

class TestRealRepoRegression:
    """MUST be scoped (not full) — these were the regressions that triggered Fix 1."""

    @pytest.fixture(scope="class")
    def real_index(self):
        tests_dir = Path(__file__).parent
        return build_import_index(tests_dir)

    @pytest.fixture(scope="class")
    def real_existing(self):
        tests_dir = Path(__file__).parent
        return {
            str(p.relative_to(tests_dir.parent))
            for p in tests_dir.rglob("test_*.py")
        }

    def test_juggle_cockpit_scopes_to_importers(self, real_index, real_existing):
        result = select_scoped_tests(
            ["src/juggle_cockpit.py"],
            real_existing,
            import_index=real_index,
        )
        assert result["mode"] == "scoped", f"Expected scoped, got full: {result['reason']}"
        assert any("test_cockpit_task_detail" in p for p in result["paths"]), (
            "test_cockpit_task_detail.py must be in scoped paths"
        )

    def test_dbops_threads_scopes_to_importers(self, real_index, real_existing):
        result = select_scoped_tests(
            ["src/dbops/threads.py"],
            real_existing,
            import_index=real_index,
        )
        assert result["mode"] == "scoped", f"Expected scoped, got full: {result['reason']}"
        # test_thread_dedup.py and test_migrations_label_retire.py import dbops.threads
        assert any("test_thread_dedup" in p for p in result["paths"]) or any(
            "label_retire" in p for p in result["paths"]
        ), f"Thread-importing tests not found in paths: {result['paths']}"

    def test_dbops_db_topics_scopes_correctly(self, real_index, real_existing):
        result = select_scoped_tests(
            ["src/dbops/db_topics.py"],
            real_existing,
            import_index=real_index,
        )
        assert result["mode"] == "scoped", f"Expected scoped, got full: {result['reason']}"


# ── apply_quarantine (Fix 2) ──────────────────────────────────────────────────

class TestApplyQuarantine:
    def test_empty_quarantine_unchanged(self):
        cmd = apply_quarantine("uv run pytest -q tests/test_a.py", [])
        assert cmd == "uv run pytest -q tests/test_a.py"

    def test_single_deselect_appended(self):
        cmd = apply_quarantine(
            "uv run pytest -q tests/test_a.py",
            ["tests/test_loc_gate.py"],
        )
        assert "--deselect tests/test_loc_gate.py" in cmd

    def test_multiple_quarantined(self):
        cmd = apply_quarantine(
            "uv run pytest -q",
            ["tests/test_loc_gate.py", "tests/test_data_migration.py"],
        )
        assert "--deselect tests/test_loc_gate.py" in cmd
        assert "--deselect tests/test_data_migration.py" in cmd

    def test_original_paths_preserved(self):
        cmd = apply_quarantine(
            "uv run pytest -q tests/test_a.py",
            ["tests/test_loc_gate.py"],
        )
        assert "tests/test_a.py" in cmd

    def test_deselect_flags_placed_before_path_args(self):
        # deselect flags must come before positional path args for pytest compat
        cmd = apply_quarantine(
            "uv run pytest -q tests/test_a.py",
            ["tests/test_loc_gate.py"],
        )
        deselect_pos = cmd.index("--deselect")
        path_pos = cmd.index("tests/test_a.py")
        assert deselect_pos < path_pos


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
