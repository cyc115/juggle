"""Conftest for schedule tests — sets _JUGGLE_TEST_DB to an in-memory path."""
import os
import tempfile
import pytest


@pytest.fixture(autouse=True)
def isolated_schedule_env(tmp_path, monkeypatch):
    """Redirect DB, state file, and reports dir for each test."""
    db_path = str(tmp_path / "juggle_test.db")
    monkeypatch.setenv("_JUGGLE_TEST_DB", db_path)
    yield
