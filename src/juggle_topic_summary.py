"""Topic summarizer — LLM-driven plain-language summary for the topic-info modal.

Pure functions (build_summarize_prompt, parse_summary_response,
format_recent_activity) are unit-testable without a network call.
summarize_topic accepts an injectable llm_fn for the same reason.
"""
from __future__ import annotations

import logging
import re
import time

_log = logging.getLogger(__name__)

_SECTIONS = ("context", "why", "what", "result")

# Strips leading markdown tokens: ##, -, *, >, **, __ (surrounding the label)
_MD_PREFIX_RE = re.compile(r"^(?:[#*\->]+ *|\*\*|__)+")
# Strips trailing **: or __ or bare : that survive prefix stripping
_MD_SUFFIX_RE = re.compile(r"[\*_]+:?$|:$")


def build_summarize_prompt(
    task_input: str,
    result_output: str,
    messages: list[dict],
    meta: dict,
) -> str:
    """Build the LLM prompt for topic summarization. Pure function."""
    label = meta.get("label", "?")
    title = meta.get("title", "") or ""
    status = meta.get("status", "") or ""

    snippets = []
    for m in messages[-10:]:
        role = (m.get("role") or "?").upper()
        content = (m.get("content") or "")[:300]
        snippets.append(f"{role}: {content}")
    msg_text = "\n".join(snippets) or "(no messages)"

    # Sub-tasks block: reflects child task-node progress so the summary tracks
    # node development, not just messages. Omitted entirely when there are none.
    child_nodes = meta.get("child_nodes") or []
    subtasks_block = ""
    if child_nodes:
        rows = "\n".join(
            f"[{c.get('id')} — {c.get('title') or ''} — {c.get('state') or ''}]"
            for c in child_nodes
        )
        subtasks_block = f"Sub-tasks:\n{rows}\n\n"

    return (
        f"You are summarizing a software development topic for someone with little context.\n"
        f"Topic: [{label}] {title} (state: {status})\n\n"
        f"Task/input (what the agent was asked to do):\n{task_input[:800] or '(none)'}\n\n"
        f"Result/output (agent's final result):\n{result_output[:800] or '(none)'}\n\n"
        f"{subtasks_block}"
        f"Recent messages:\n{msg_text}\n\n"
        f"Write exactly 4 short sections (1-4 sentences each). Plain language.\n"
        f"IMPORTANT: output headers as PLAIN TEXT only — no bold, no markdown, no bullets, "
        f"no preamble line. Use exactly this format:\n"
        f"Context: <surrounding situation — what problem/project this belongs to>\n"
        f"Why: <why this change/work was needed>\n"
        f"What: <what the change actually is, in simple plain language>\n"
        f"Result: <final outcome — completed? issues? any follow-up needed?>"
    )


def _normalize_line(line: str) -> tuple[str | None, str]:
    """Normalize a line for header matching.

    Returns (matched_key, remainder_text) or (None, "") if no header matched.
    Handles: bare "Key:", **Key:**, **Key**:, ## Key:, - Key:, KEY:, Key - text.
    """
    stripped = line.strip()
    if not stripped:
        return None, ""

    # Strip leading markdown tokens (##, -, *, >, **, __)
    normalized = _MD_PREFIX_RE.sub("", stripped).strip()

    for key in _SECTIONS:
        label = key.capitalize()
        # Allow optional closing **/__  before colon: "Context**:" or "Context**:" or "Context:"
        # Colon separator
        m = re.match(rf"(?i)^{label}(?:\*\*|__)?\s*:", normalized)
        if m:
            remainder = normalized[m.end():].strip()
            # Strip any leading ** or __ from remainder (e.g. "**:" edge case)
            remainder = re.sub(r"^\*\*|^__", "", remainder).strip()
            return key, remainder
        # Dash separator: "Context - text"
        m = re.match(rf"(?i)^{label}(?:\*\*|__)?\s+-\s*(.*)", normalized)
        if m:
            return key, m.group(1).strip()

    return None, ""


def parse_summary_response(text: str) -> dict[str, str]:
    """Parse LLM response into {context, why, what, result}. Pure function.

    Robust to markdown-wrapped headers: **Context:**, ## Why:, - What:, etc.
    Case-insensitive. Tolerates a preamble line before the first header.
    """
    sections: dict[str, str] = {s: "" for s in _SECTIONS}
    if not text:
        return sections

    current: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        if current:
            sections[current] = " ".join(buf).strip()

    for line in text.splitlines():
        key, remainder = _normalize_line(line)
        if key is not None:
            _flush()
            current = key
            buf = [remainder] if remainder else []
        elif current:
            stripped = line.strip()
            if stripped:
                buf.append(stripped)

    _flush()
    return sections


def summarize_topic(
    task_input: str,
    result_output: str,
    messages: list[dict],
    meta: dict,
    llm_fn=None,
) -> dict[str, str]:
    """Call LLM to produce {context, why, what, result} for a topic.

    llm_fn(prompt: str) -> str | None — injectable for tests (default: llm_call cheap).
    Returns empty-string dict on any error so modal can show raw fallback.
    """
    if llm_fn is None:
        from llm_calls import llm_call

        def llm_fn(prompt: str) -> str | None:
            t0 = time.monotonic()
            result = llm_call(prompt, profile="cheap", timeout=30)
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # provider logged inside llm_call; log elapsed here for the modal path
            _log.info(
                "summarize_topic: llm_call elapsed=%dms result_len=%d",
                elapsed_ms,
                len(result) if result else 0,
            )
            return result

    try:
        prompt = build_summarize_prompt(task_input, result_output, messages, meta)
        t0 = time.monotonic()
        text = llm_fn(prompt) or ""
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log.debug(
            "summarize_topic: raw response len=%d first200=%r elapsed=%dms",
            len(text),
            text[:200],
            elapsed_ms,
        )
        sections = parse_summary_response(text)
        filled = sum(1 for v in sections.values() if v.strip())
        if filled == 4:
            _log.info("summarize_topic: parsed %d/4 sections OK", filled)
        else:
            _log.warning(
                "summarize_topic: parsed %d/4 sections — fell back to raw display (response: %r)",
                filled,
                text[:200],
            )
        return sections
    except Exception as exc:
        _log.warning("summarize_topic failed: %s", exc)
        return {s: "" for s in _SECTIONS}


def format_recent_activity(messages: list[dict], limit: int = 5) -> list[str]:
    """Return trimmed one-line bullets for the last `limit` messages. Pure function."""
    if not messages:
        return []
    recent = messages[-limit:]
    lines = []
    for m in recent:
        role = m.get("role") or "?"
        content = (m.get("content") or "").strip().replace("\n", " ")
        excerpt = content[:120]
        if len(content) > 120:
            excerpt += "…"
        lines.append(f"[{role}] {excerpt}")
    return lines
