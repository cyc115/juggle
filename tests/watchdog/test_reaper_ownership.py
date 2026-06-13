"""Assert watchdog _poll_once calls reap_stale_agents; CLI/cmd_agents do not."""
import ast
import sys
from pathlib import Path
from unittest import mock

SRC_DIR = str(Path(__file__).parent.parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
_SRC_DIR = Path(__file__).parent.parent.parent / "src"


def _load_watchdog_module():
    """Load the watchdog daemon-loop module fresh with its deps mocked.

    Daemon logic moved from scripts/juggle-agent-watchdog to
    src/juggle_watchdog_daemon.py in the 2026-06-10 refactor; same assertions
    through the new seam (the script is now a thin wrapper).
    """
    import importlib.machinery
    import importlib.util
    import tempfile, logging

    loader = importlib.machinery.SourceFileLoader(
        "juggle_agent_watchdog",
        str(_SRC_DIR / "juggle_watchdog_daemon.py"),
    )
    spec = importlib.util.spec_from_loader("juggle_agent_watchdog", loader)
    mod = importlib.util.module_from_spec(spec)

    settings_mock = mock.MagicMock()
    settings_mock.__getitem__ = mock.Mock(side_effect=lambda k: {
        "paths": {"config_dir": tempfile.mkdtemp()},
        "agent_boot_grace_secs": 120,
    }.get(k, mock.MagicMock()))
    settings_mock.get = mock.Mock(return_value=120)

    juggle_settings_mock = mock.MagicMock()
    juggle_settings_mock.get_settings = mock.Mock(return_value=settings_mock)

    _mocks = {
        "juggle_db": mock.MagicMock(),
        "juggle_settings": juggle_settings_mock,
        "juggle_tmux": mock.MagicMock(),
        "juggle_watchdog": mock.MagicMock(),
        "juggle_watchdog_health": mock.MagicMock(),
    }
    with mock.patch.dict("sys.modules", _mocks):
        spec.loader.exec_module(mod)
    return mod


def test_poll_once_calls_reap_stale_agents():
    """_poll_once must call reap_stale_agents so watchdog is sole periodic reaper."""
    mod = _load_watchdog_module()

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_db.get_all_agents.return_value = []

    reap_mock = mock.MagicMock()
    juggle_tmux_mock = mock.MagicMock(reap_stale_agents=reap_mock)

    with mock.patch.dict("sys.modules", {"juggle_tmux": juggle_tmux_mock}):
        with mock.patch.object(mod, "check_orphaned_threads"):
            with mock.patch.object(mod, "get_session_id", return_value="s1"):
                with mock.patch.object(mod, "write_heartbeat", mock.MagicMock()):
                    mod._poll_once(mock_db, mock_mgr)

    reap_mock.assert_called_once_with(mock_db, mock_mgr)


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for task in ast.walk(tree):
        if isinstance(task, ast.FunctionDef) and task.name == name:
            return task
    return None


def _reap_calls_in(func: ast.FunctionDef) -> list:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "reap_stale_agents"
    ]


def test_cli_main_does_not_call_reap_stale_agents():
    """juggle_cli main() must not call reap_stale_agents — watchdog owns periodic reap."""
    source = (_SRC_DIR / "juggle_cli.py").read_text()
    tree = ast.parse(source)

    main_func = _find_func(tree, "main")
    assert main_func is not None, "main() not found in juggle_cli.py"

    calls = _reap_calls_in(main_func)
    assert len(calls) == 0, (
        f"main() still calls reap_stale_agents {len(calls)} time(s) — remove it"
    )


def test_cmd_get_agent_does_not_call_reap_stale_agents():
    """cmd_get_agent must not call reap_stale_agents — watchdog owns periodic reap."""
    # cmd_get_agent moved to juggle_cmd_agents_lifecycle.py in the 2026-06-10
    # cmd_agents split; same behavior pinned through the new seam.
    source = (_SRC_DIR / "juggle_cmd_agents_lifecycle.py").read_text()
    tree = ast.parse(source)

    func = _find_func(tree, "cmd_get_agent")
    assert func is not None, "cmd_get_agent() not found in juggle_cmd_agents_lifecycle.py"

    calls = _reap_calls_in(func)
    assert len(calls) == 0, (
        f"cmd_get_agent still calls reap_stale_agents {len(calls)} time(s) — remove it"
    )
