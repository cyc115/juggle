"""
TDD test: juggle_hooks must ignore CLAUDE_PLUGIN_DATA (leaks from codex plugin)
and always use the canonical config path from _get_settings().
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

def test_resolve_data_dir_ignores_claude_plugin_data_env(monkeypatch):
    """CLAUDE_PLUGIN_DATA set to a bogus codex path must not affect data_dir."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/tmp/bogus_codex_dir")

    # Import after env is set so module-level code sees the env if not fixed.
    # Use importlib.reload to re-evaluate module-level _DATA_DIR each run.
    import importlib
    import juggle_hooks

    importlib.reload(juggle_hooks)

    expected = Path("/Users/mikechen/.claude/juggle")
    assert juggle_hooks._DATA_DIR == expected, (
        f"Expected {expected}, got {juggle_hooks._DATA_DIR}. "
        "CLAUDE_PLUGIN_DATA from codex plugin must not override juggle's data_dir."
    )
