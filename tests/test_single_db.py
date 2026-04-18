"""Regression test: DB_PATH is invariant under CLAUDE_PLUGIN_DATA."""

import os
import sys
from pathlib import Path
from unittest import mock

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_db_path_ignores_claude_plugin_data():
    """Verify DB_PATH doesn't change when CLAUDE_PLUGIN_DATA env var is set."""
    from juggle_cli_common import DB_PATH as original_db_path

    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": "/tmp/fake/path"}):
        import importlib
        import juggle_cli_common
        importlib.reload(juggle_cli_common)
        from juggle_cli_common import DB_PATH as mocked_db_path

        assert str(mocked_db_path).endswith(".claude/juggle/juggle.db")
        assert mocked_db_path == original_db_path


def test_db_path_resolves_absolute():
    """Verify DB_PATH is absolute and properly expanded."""
    from juggle_cli_common import DB_PATH
    db_path = Path(DB_PATH)
    assert db_path.is_absolute(), f"DB_PATH not absolute: {db_path}"
    assert str(db_path).endswith("juggle.db")
