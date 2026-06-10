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


def _attribute_tool_errors(tool_uses: list[dict], tool_results: list[dict]) -> None:
    """N=10 same-turn causal attribution."""
    from juggle_selfheal import record_orchestration_error

    N = 10
    recent_uses = tool_uses[-N:]
    recent_inputs_str = " ".join(json.dumps(tc.get("input") or {}) for tc in recent_uses)

    juggle_ref: str | None = None
    for path in _JUGGLE_PATHS:
        if path in recent_inputs_str:
            juggle_ref = path
            break

    if juggle_ref is None:
        return

    use_by_id = {tc.get("id"): tc for tc in tool_uses}

    for tr in tool_results:
        if tr.get("is_error") is not True:
            continue
        error_text = str(tr.get("content", ""))
        use_id = tr.get("tool_use_id")
        tc = use_by_id.get(use_id, {})
        tool_name = tc.get("name", "unknown")
        tool_input = tc.get("input") or {}
        record_orchestration_error(tool_name, tool_input, error_text, juggle_ref)
