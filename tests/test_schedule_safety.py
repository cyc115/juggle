#!/usr/bin/env python3
"""TDD tests for scheduler clobbering safety fixes.

Fix 1: busy-agent gate in dogfood/reflect/autofix run()
Fix 2: git_commit(paths=...) to stage only specific files
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import schedules.common as jsc


# ---------------------------------------------------------------------------
# Fix 1a: has_busy_agents() helper in juggle_schedule_common
# ---------------------------------------------------------------------------

class TestHasBusyAgents:
    def test_returns_true_when_busy_agent_exists(self):
        """Returns True when at least one agent has status='busy'"""
        db = MagicMock()
        with patch.object(jsc, "db_query", return_value=[{"id": "agent-1"}]):
            assert jsc.has_busy_agents(db) is True

    def test_returns_false_when_no_busy_agents(self):
        """Returns False when agents table has no busy rows"""
        db = MagicMock()
        with patch.object(jsc, "db_query", return_value=[]):
            assert jsc.has_busy_agents(db) is False

    def test_returns_false_on_db_exception(self):
        """Returns False (safe default) when DB query raises"""
        db = MagicMock()
        with patch.object(jsc, "db_query", side_effect=Exception("db error")):
            assert jsc.has_busy_agents(db) is False

    def test_queries_agents_table_by_busy_status(self):
        """Issues query that filters agents on status='busy'"""
        db = MagicMock()
        with patch.object(jsc, "db_query", return_value=[]) as mock_q:
            jsc.has_busy_agents(db)
        sql = mock_q.call_args[0][1]
        assert "busy" in sql.lower()
        assert "agents" in sql.lower()


# ---------------------------------------------------------------------------
# Fix 2: git_commit(paths=...) in juggle_schedule_common
# ---------------------------------------------------------------------------

class TestGitCommitPaths:
    def _make_side_effects(self, diff_rc: int = 1):
        """add → diff --cached → commit"""
        return [
            MagicMock(returncode=0),       # git add
            MagicMock(returncode=diff_rc), # git diff --cached --quiet (1=has changes)
            MagicMock(returncode=0),       # git commit
        ]

    def test_no_paths_stages_all(self):
        """Backward compat: omitting paths uses git add -A"""
        with patch.object(jsc, "git_run", side_effect=self._make_side_effects()) as mock_git:
            jsc.git_commit("msg")
        add_args = mock_git.call_args_list[0][0][0]
        assert add_args == ["add", "-A"]

    def test_explicit_paths_stages_only_those_files(self):
        """With paths=, stages only the named files"""
        with patch.object(jsc, "git_run", side_effect=self._make_side_effects()) as mock_git:
            jsc.git_commit("msg", paths=["reports/dogfood-2026-06-05.md"])
        add_args = mock_git.call_args_list[0][0][0]
        assert add_args == ["add", "--", "reports/dogfood-2026-06-05.md"]

    def test_explicit_paths_multiple_files(self):
        """Multiple paths are all passed to git add"""
        files = ["reports/a.md", "reports/b.md"]
        with patch.object(jsc, "git_run", side_effect=self._make_side_effects()) as mock_git:
            jsc.git_commit("msg", paths=files)
        add_args = mock_git.call_args_list[0][0][0]
        assert add_args == ["add", "--", "reports/a.md", "reports/b.md"]

    def test_explicit_paths_nothing_to_commit_returns_false(self):
        """Returns False when explicit paths yield no staged diff"""
        side_effects = [
            MagicMock(returncode=0),  # git add
            MagicMock(returncode=0),  # diff --cached --quiet (0=nothing staged)
        ]
        with patch.object(jsc, "git_run", side_effect=side_effects):
            result = jsc.git_commit("msg", paths=["reports/x.md"])
        assert result is False


# ---------------------------------------------------------------------------
# Fix 1b: busy-agent gate in each schedule module's run()
# ---------------------------------------------------------------------------

class TestDogfoodBusyAgentGate:
    def test_run_returns_1_when_busy_agents(self):
        """dogfood run() exits early (rc=1) when agents are busy"""
        import schedules.dogfood as dogfood
        mock_db = MagicMock()
        with patch.object(dogfood, "get_db", return_value=mock_db), \
             patch.object(dogfood, "has_busy_agents", return_value=True), \
             patch.object(dogfood, "_ensure_reports_dir"):
            result = dogfood.run(dry_run=False)
        assert result == 1

    def test_run_proceeds_when_no_busy_agents(self):
        """dogfood run() does NOT exit early when no busy agents"""
        import schedules.dogfood as dogfood
        mock_db = MagicMock()
        # Gate passes; mock the rest of run() to prevent actual execution
        with patch.object(dogfood, "get_db", return_value=mock_db), \
             patch.object(dogfood, "has_busy_agents", return_value=False), \
             patch.object(dogfood, "_ensure_reports_dir"), \
             patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
             patch.object(dogfood, "_check_active_session", return_value=False), \
             patch.object(dogfood, "_tmux_session_exists", return_value=False), \
             patch.object(dogfood, "_run_headless_research", return_value="output"), \
             patch.object(dogfood, "write_report"), \
             patch.object(dogfood, "git_commit", return_value=False), \
             patch.object(dogfood, "_file_action_item"), \
             patch.object(dogfood, "_find_or_create_schedule_thread", return_value=None), \
             patch.object(dogfood, "mark_run_complete"):
            result = dogfood.run(dry_run=False)
        # Should not return 1 from the busy-agent gate
        assert result == 0


class TestReflectBusyAgentGate:
    def test_run_returns_1_when_busy_agents(self):
        """reflect run() exits early (rc=1) when agents are busy"""
        import schedules.reflect as reflect
        mock_db = MagicMock()
        with patch.object(reflect, "get_db", return_value=mock_db), \
             patch.object(reflect, "has_busy_agents", return_value=True):
            result = reflect.run(dry_run=False)
        assert result == 1

    def test_run_proceeds_when_no_busy_agents(self):
        """reflect run() does NOT exit early when no busy agents"""
        import schedules.reflect as reflect
        mock_db = MagicMock()
        with patch.object(reflect, "get_db", return_value=mock_db), \
             patch.object(reflect, "has_busy_agents", return_value=False), \
             patch.object(reflect, "rf1_watchdog"), \
             patch.object(reflect, "rf2_action_items"), \
             patch.object(reflect, "rf3_completion_quality"), \
             patch.object(reflect, "rf4_context_bloat"), \
             patch.object(reflect, "rf5_hindsight_lint"), \
             patch.object(reflect, "rf6_auto_memory"), \
             patch.object(reflect, "rf7_skill_drift"), \
             patch.object(reflect, "rf8_dogfood_pulse"), \
             patch.object(reflect, "_build_digest", return_value="digest"), \
             patch.object(reflect, "_find_autofix_pr_ref", return_value=None), \
             patch.object(reflect, "write_report"), \
             patch.object(reflect, "git_commit", return_value=False), \
             patch.object(reflect, "_file_reflect_issues", return_value=[]), \
             patch.object(reflect, "mark_run_complete"):
            result = reflect.run(dry_run=False)
        assert result == 0


class TestAutofixBusyAgentGate:
    def test_run_returns_1_when_busy_agents(self):
        """autofix run() exits early (rc=1) when agents are busy"""
        import schedules.autofix as autofix
        mock_db = MagicMock()
        with patch.object(autofix, "get_db", return_value=mock_db), \
             patch.object(autofix, "has_busy_agents", return_value=True):
            result = autofix.run(dry_run=False)
        assert result == 1

    def test_run_proceeds_when_no_busy_agents(self):
        """autofix run() does NOT exit early when no busy agents"""
        import schedules.autofix as autofix
        mock_db = MagicMock()
        with patch.object(autofix, "get_db", return_value=mock_db), \
             patch.object(autofix, "has_busy_agents", return_value=False), \
             patch.object(autofix, "gh_pr_list_head", return_value=[]), \
             patch.object(autofix, "git_run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch.object(autofix, "fx1_ruff"), \
             patch.object(autofix, "fx2_vulture"), \
             patch.object(autofix, "fx3_test_gaps"), \
             patch.object(autofix, "fx4_watchdog_tests"), \
             patch.object(autofix, "fx5_doc_drift", return_value=("", None)), \
             patch.object(autofix, "fx6_changelog"), \
             patch.object(autofix, "fx7_graphify"), \
             patch.object(autofix, "is1_bandit"), \
             patch.object(autofix, "is2_skill_audit"), \
             patch.object(autofix, "_smoke_test", return_value=True), \
             patch.object(autofix, "_read_dogfood_snippet", return_value=""), \
             patch.object(autofix, "_build_pr_description", return_value="pr body"), \
             patch.object(autofix, "mark_run_complete"), \
             patch("subprocess.run") as mock_subp:
            mock_subp.return_value = MagicMock(returncode=0, stdout="https://github.com/pr/1", stderr="")
            result = autofix.run(dry_run=False)
        # Proceeds past busy-agent gate → reaches end of run() → returns 0
        assert result == 0


# ---------------------------------------------------------------------------
# Fix 2b: dogfood and reflect pass explicit paths to git_commit
# ---------------------------------------------------------------------------

class TestDogfoodPassesExplicitPaths:
    def test_git_commit_called_with_report_path(self):
        """dogfood passes the report file path to git_commit"""
        import schedules.dogfood as dogfood
        mock_db = MagicMock()
        captured_calls = []

        def capture_commit(msg, paths=None, cwd=None):
            captured_calls.append({"msg": msg, "paths": paths})
            return False  # nothing committed

        with patch.object(dogfood, "get_db", return_value=mock_db), \
             patch.object(dogfood, "has_busy_agents", return_value=False), \
             patch.object(dogfood, "_ensure_reports_dir"), \
             patch.object(dogfood, "_check_prior_dogfood_thread", return_value=None), \
             patch.object(dogfood, "_check_active_session", return_value=False), \
             patch.object(dogfood, "_tmux_session_exists", return_value=False), \
             patch.object(dogfood, "_run_headless_research", return_value="output"), \
             patch.object(dogfood, "write_report"), \
             patch.object(dogfood, "git_commit", side_effect=capture_commit), \
             patch.object(dogfood, "_file_action_item"), \
             patch.object(dogfood, "_find_or_create_schedule_thread", return_value=None), \
             patch.object(dogfood, "mark_run_complete"):
            dogfood.run(dry_run=False)

        assert len(captured_calls) >= 1
        first = captured_calls[0]
        assert first["paths"] is not None, "git_commit must receive explicit paths"
        assert any("dogfood" in p for p in first["paths"]), \
            f"paths should reference the dogfood report; got {first['paths']}"


class TestReflectPassesExplicitPaths:
    def test_git_commit_called_with_report_path(self):
        """reflect passes the report file path to git_commit"""
        import schedules.reflect as reflect
        mock_db = MagicMock()
        captured_calls = []

        def capture_commit(msg, paths=None, cwd=None):
            captured_calls.append({"msg": msg, "paths": paths})
            return False

        with patch.object(reflect, "get_db", return_value=mock_db), \
             patch.object(reflect, "has_busy_agents", return_value=False), \
             patch.object(reflect, "rf1_watchdog"), \
             patch.object(reflect, "rf2_action_items"), \
             patch.object(reflect, "rf3_completion_quality"), \
             patch.object(reflect, "rf4_context_bloat"), \
             patch.object(reflect, "rf5_hindsight_lint"), \
             patch.object(reflect, "rf6_auto_memory"), \
             patch.object(reflect, "rf7_skill_drift"), \
             patch.object(reflect, "rf8_dogfood_pulse"), \
             patch.object(reflect, "_build_digest", return_value="digest"), \
             patch.object(reflect, "_find_autofix_pr_ref", return_value=None), \
             patch.object(reflect, "write_report"), \
             patch.object(reflect, "git_commit", side_effect=capture_commit), \
             patch.object(reflect, "_file_reflect_issues", return_value=[]), \
             patch.object(reflect, "mark_run_complete"):
            reflect.run(dry_run=False)

        assert len(captured_calls) >= 1
        first = captured_calls[0]
        assert first["paths"] is not None, "git_commit must receive explicit paths"
        assert any("reflect" in p for p in first["paths"]), \
            f"paths should reference the reflect report; got {first['paths']}"
