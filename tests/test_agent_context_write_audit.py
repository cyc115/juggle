"""Closes the DA constraint 'no agent-context write path to the DB remains' —
proves every spooled command (Tasks 3-6) is unreachable to a write."""
import pytest


@pytest.mark.parametrize("module,func", [
    ("juggle_cmd_agents_complete", "cmd_complete_agent"),
    ("juggle_cmd_agents_complete", "cmd_fail_agent"),
    ("juggle_cmd_agents", "cmd_request_action"),
    ("juggle_cmd_agents", "cmd_ack_action"),
    ("juggle_cmd_agents", "cmd_notify"),
    ("juggle_cmd_graph", "cmd_graph_mark_task"),
])
def test_agent_context_write_commands_never_reach_get_db_write(module, func, monkeypatch):
    """Static-ish audit: import each handler, monkeypatch should_spool True, and
    assert get_db is never invoked with the module's own get_db symbol."""
    import importlib

    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    mod = importlib.import_module(module)
    called = []
    target_name = "get_db" if hasattr(mod, "get_db") else None
    if target_name:
        monkeypatch.setattr(mod, target_name, lambda *a, **k: called.append(1) or (_ for _ in ()).throw(RuntimeError))
    import juggle_cli_common as cc
    monkeypatch.setattr(cc, "get_db", lambda *a, **k: called.append(1) or (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr("dbops.spool.write_event", lambda *a, **k: "fake-uuid")
    monkeypatch.setattr("juggle_watchdog_poke.poke_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(cc, "resolve_thread_id_for_spool", lambda s: s)

    import argparse
    handler = getattr(mod, func)
    args = argparse.Namespace(
        thread_id="AB", result_summary="x", retain_text=None, open_questions=None,
        handoff=None, role=None, error="e", failure_type=None, max_retries=0,
        recovery_dispatched=False, message="m", type="manual_step", priority="normal",
        action_id="1", task_id="t", fail=False,
    )
    handler(args)
    assert called == [], f"{module}.{func} reached a get_db() write path in agent context"
