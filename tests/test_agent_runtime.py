"""Agent model/effort cascade resolver (2026-06-30 agent model/effort config).

Cascade, lowest→highest precedence:
  built-in default → agents.model/effort (global) → agents.by_role[role] → per-dispatch flag.
"""
from juggle_agent_runtime import resolve_agent_runtime


def _s(agents):
    return {"agents": agents}


def test_nothing_set_uses_builtin_default():
    got = resolve_agent_runtime("coder", settings={})
    assert got == {"model": "sonnet", "effort": None}


def test_global_model_and_effort():
    got = resolve_agent_runtime("coder", settings=_s({"model": "opus", "effort": "high"}))
    assert got == {"model": "opus", "effort": "high"}


def test_by_role_overrides_global():
    s = _s({"model": "opus", "effort": "high",
            "by_role": {"coder": {"model": "haiku", "effort": "low"}}})
    assert resolve_agent_runtime("coder", settings=s) == {"model": "haiku", "effort": "low"}


def test_role_absent_from_by_role_falls_to_global():
    s = _s({"model": "opus", "effort": "high",
            "by_role": {"coder": {"model": "haiku"}}})
    assert resolve_agent_runtime("planner", settings=s) == {"model": "opus", "effort": "high"}


def test_dispatch_flag_overrides_everything():
    s = _s({"model": "opus", "effort": "high",
            "by_role": {"coder": {"model": "haiku", "effort": "low"}}})
    got = resolve_agent_runtime("coder", model_flag="fable-5", effort_flag="max", settings=s)
    assert got == {"model": "fable-5", "effort": "max"}


def test_mixed_flag_model_with_by_role_effort():
    s = _s({"by_role": {"coder": {"effort": "xhigh"}}})
    got = resolve_agent_runtime("coder", model_flag="opus", settings=s)
    assert got == {"model": "opus", "effort": "xhigh"}


def test_none_role_uses_global_only():
    s = _s({"model": "opus", "by_role": {"coder": {"model": "haiku"}}})
    assert resolve_agent_runtime(None, settings=s) == {"model": "opus", "effort": None}
