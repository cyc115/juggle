"""
juggle_hooks_classb — Class B transcript scan (Stop-hook, Juggle-caused errors).

Owns: _scan_transcript_for_class_b, _do_class_b_scan, _attribute_tool_errors.
Must not own: DB path constants, handler dispatch, checkpoint logic.
"""

import json
import logging
from pathlib import Path


_JUGGLE_PATHS: tuple[str, ...] = (
    "juggle_cli.py",
    "juggle_hooks.py",
    "juggle_selfheal.py",
    "scripts/juggle-",
    "commands/",
    "juggle:",
)

_MAX_TRANSCRIPT_LINES = 200


def _scan_transcript_for_class_b(data: dict) -> None:
    """Called from handle_stop(). Silently skips if no transcript_path."""
    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return
    try:
        _do_class_b_scan(Path(transcript_path))
    except Exception as exc:
        logging.warning("Class B transcript scan failed: %s", exc)


def _do_class_b_scan(transcript_path: Path) -> None:
    """Parse transcript JSONL and record tool errors attributed to Juggle.

    Verified real schema (2026-05-30):
    - type="user" with message.content=str → human turn boundary
    - type="assistant" → tool_use blocks in message.content list
    - type="user" with message.content=list → tool_result blocks
    - tool_use: {type, id, name, input, caller}
    - tool_result: {type, tool_use_id, is_error, content}
    - is_error is True for errors; False or None for success
    """
    all_lines = transcript_path.read_text(errors="replace").splitlines()
    lines = all_lines[-_MAX_TRANSCRIPT_LINES:]

    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Find last human-text turn boundary
    boundary_idx = -1
    for i, rec in enumerate(records):
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", "")
        if isinstance(content, str):
            boundary_idx = i
        elif isinstance(content, list):
            if any(isinstance(x, dict) and x.get("type") == "text" for x in content):
                boundary_idx = i

    if boundary_idx < 0:
        return

    current_turn = records[boundary_idx + 1:]

    tool_uses: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_uses.append(item)

    tool_results: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_results.append(item)

    _attribute_tool_errors(tool_uses, tool_results)


def _attribute_tool_errors(tool_uses: list[dict], tool_results: list[dict]) -> int:
    """N=10 same-turn causal attribution.

    Returns the number of EXPECTED orchestrator hook-deny blocks suppressed
    (Task 7) — kept OBSERVABLE (counted + logged) rather than silently dropped,
    so the noise class stays measurable.
    """
    from juggle_selfheal import record_orchestration_error
    from selfheal_triage import is_expected_hook_block

    N = 10
    recent_uses = tool_uses[-N:]
    recent_inputs_str = " ".join(json.dumps(tc.get("input") or {}) for tc in recent_uses)

    juggle_ref: str | None = None
    for path in _JUGGLE_PATHS:
        if path in recent_inputs_str:
            juggle_ref = path
            break

    if juggle_ref is None:
        return 0

    use_by_id = {tc.get("id"): tc for tc in tool_uses}

    suppressed = 0
    for tr in tool_results:
        if tr.get("is_error") is not True:
            continue
        error_text = str(tr.get("content", ""))
        # Expected PreToolUse deny-blocks are policy, not Juggle bugs — skip them
        # at the capture boundary (Task 7), but count for observability.
        if is_expected_hook_block(error_text):
            suppressed += 1
            continue
        use_id = tr.get("tool_use_id")
        tc = use_by_id.get(use_id, {})
        tool_name = tc.get("name", "unknown")
        tool_input = tc.get("input") or {}
        record_orchestration_error(tool_name, tool_input, error_text, juggle_ref)
    if suppressed:
        logging.info("selfheal.hookblock suppressed %d expected deny-block(s)", suppressed)
    return suppressed
