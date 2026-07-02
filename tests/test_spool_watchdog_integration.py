"""Task 9 of the spool-single-writer plan: drain_spool wired into the watchdog
tick (_poll_once) and into main() at startup (drain-on-start), so events
spooled by an agent-context write (P8 collapse) get applied without waiting on
a live agent-context caller.

Regression scope: agent-context callers spool events (Task 7/8) but nothing
was replaying them — _poll_once must drain every tick, and main() must drain
once at boot so events written while the watchdog was down aren't stranded
until the next tick.
"""
import ast
import sys
from pathlib import Path
from unittest import mock

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

_SRC_DIR = Path(__file__).parent.parent / "src"


def _load_watchdog_module():
    """Load juggle_watchdog_daemon.py fresh with heavy deps mocked (same seam
    used by tests/watchdog/test_reaper_ownership.py)."""
    import importlib.machinery
    import importlib.util
    import tempfile

    loader = importlib.machinery.SourceFileLoader(
        "juggle_agent_watchdog_spooltest",
        str(_SRC_DIR / "juggle_watchdog_daemon.py"),
    )
    spec = importlib.util.spec_from_loader("juggle_agent_watchdog_spooltest", loader)
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


def test_poll_once_calls_drain_spool():
    """_poll_once must drain the spool every tick so agent-context spooled
    events (record_error, agent_complete, action_notify, ...) get applied
    without waiting for a live agent-context caller."""
    mod = _load_watchdog_module()

    mock_db = mock.MagicMock()
    mock_mgr = mock.MagicMock()
    mock_db.get_all_agents.return_value = []

    with mock.patch.object(mod, "drain_spool") as drain_mock:
        with mock.patch.object(mod, "check_orphaned_threads"):
            with mock.patch.object(mod, "get_session_id", return_value="s1"):
                with mock.patch.object(mod, "write_heartbeat", mock.MagicMock()):
                    mod._poll_once(mock_db, mock_mgr)

    drain_mock.assert_called_once_with(mock_db)


def _find_func(tree: ast.AST, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls_named(func: ast.FunctionDef, name: str) -> list:
    return [
        n for n in ast.walk(func)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    ]


def test_main_drains_spool_on_start_before_loop():
    """main() must drain-on-start: a drain_spool(db) call after db.init_db()
    but before the `while _running:` tick loop, so events spooled while the
    watchdog was down are applied at boot rather than stranded until tick 1
    (which itself now also drains — this pins the *startup* drain exists)."""
    source = (_SRC_DIR / "juggle_watchdog_daemon.py").read_text()
    tree = ast.parse(source)

    main_func = _find_func(tree, "main")
    assert main_func is not None, "main() not found in juggle_watchdog_daemon.py"

    drain_calls = _calls_named(main_func, "drain_spool")
    assert len(drain_calls) >= 1, "main() must call drain_spool(...) once at startup"

    while_nodes = [n for n in ast.walk(main_func) if isinstance(n, ast.While)]
    assert while_nodes, "main() must contain the tick `while _running:` loop"
    while_line = while_nodes[0].lineno

    assert any(c.lineno < while_line for c in drain_calls), (
        "drain_spool(...) in main() must run BEFORE the while-loop (drain-on-start), "
        "not merely inside it"
    )
