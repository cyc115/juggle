"""selfheal v2 p2 Task 7 — expected orchestrator hook-deny blocks are NOT captured."""
from selfheal_triage import is_expected_hook_block


def test_is_expected_hook_block_matches_deny_message():
    # Robust Juggle-owned phrase (the deny-hook systemMessage):
    assert is_expected_hook_block("🚫 Write blocked in juggle orchestrator session [abc123]. Use ...")
    # Robust structured marker (permissionDecision: deny), independent of prose:
    assert is_expected_hook_block('{"hookSpecificOutput": {"permissionDecision": "deny"}} Bash blocked')
    # Real tool errors must NOT match:
    assert not is_expected_hook_block("bash: foo: command not found")
    assert not is_expected_hook_block("KeyError: 'agent'")
    assert not is_expected_hook_block("")


def test_attribute_skips_expected_hook_block_with_observability(monkeypatch):
    """REGRESSION (2026-06-21 selfheal v2 p2): expected orchestrator deny-blocks
    were captured as B errors, inflating the queue with policy decisions. They are
    now filtered at the capture boundary — but suppression stays OBSERVABLE
    (counted), not silently dropped (DA fix e)."""
    import juggle_hooks_classb as hc
    calls = []
    monkeypatch.setattr("juggle_selfheal.record_orchestration_error",
                        lambda *a, **k: calls.append(a))
    uses = [{"id": "u1", "name": "Write", "input": {"file_path": "juggle_cli.py"}}]
    results = [{"type": "tool_result", "tool_use_id": "u1", "is_error": True,
                "content": "🚫 Write blocked in juggle orchestrator session [s1]."}]
    suppressed = hc._attribute_tool_errors(uses, results)
    assert calls == []          # expected deny-block NOT recorded
    assert suppressed == 1      # but it is OBSERVABLE (counted)


def test_attribute_still_captures_genuine_tool_error(monkeypatch):
    import juggle_hooks_classb as hc
    calls = []
    monkeypatch.setattr("juggle_selfheal.record_orchestration_error",
                        lambda *a, **k: calls.append(a))
    uses = [{"id": "u1", "name": "Bash", "input": {"command": "python juggle_cli.py x"}}]
    results = [{"type": "tool_result", "tool_use_id": "u1", "is_error": True,
                "content": "bash: foo: command not found"}]
    suppressed = hc._attribute_tool_errors(uses, results)
    assert len(calls) == 1      # genuine error still captured
    assert suppressed == 0
