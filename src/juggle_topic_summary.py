"""Topic summarizer — LLM-driven plain-language summary for the topic-info modal.

Pure functions (build_summarize_prompt, parse_summary_response,
format_recent_activity) are unit-testable without a network call.
summarize_topic accepts an injectable llm_fn for the same reason.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

_SECTIONS = ("context", "why", "what", "result")


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

    return (
        f"You are summarizing a software development topic for someone with little context.\n"
        f"Topic: [{label}] {title} (state: {status})\n\n"
        f"Task/input (what the agent was asked to do):\n{task_input[:800] or '(none)'}\n\n"
        f"Result/output (agent's final result):\n{result_output[:800] or '(none)'}\n\n"
        f"Recent messages:\n{msg_text}\n\n"
        f"Write exactly 4 short sections (1-4 sentences each). Plain language.\n"
        f"Format:\n"
        f"Context: <surrounding situation — what problem/project this belongs to>\n"
        f"Why: <why this change/work was needed>\n"
        f"What: <what the change actually is, in simple plain language>\n"
        f"Result: <final outcome — completed? issues? any follow-up needed?>"
    )


def parse_summary_response(text: str) -> dict[str, str]:
    """Parse LLM response into {{context, why, what, result}}. Pure function."""
    sections: dict[str, str] = {s: "" for s in _SECTIONS}
    if not text:
        return sections

    current: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        if current:
            sections[current] = " ".join(buf).strip()

    for line in text.splitlines():
        matched = False
        for key in _SECTIONS:
            prefix = f"{key.capitalize()}:"
            if line.startswith(prefix):
                _flush()
                current = key
                buf = [line[len(prefix):].strip()]
                matched = True
                break
        if not matched and current:
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
    """Call LLM to produce {{context, why, what, result}} for a topic.

    llm_fn(prompt: str) -> str | None — injectable for tests (default: llm_call cheap).
    Returns empty-string dict on any error so modal can show raw fallback.
    """
    if llm_fn is None:
        from llm_calls import llm_call

        def llm_fn(prompt: str) -> str | None:
            return llm_call(prompt, profile="cheap", timeout=30)

    try:
        prompt = build_summarize_prompt(task_input, result_output, messages, meta)
        text = llm_fn(prompt) or ""
        return parse_summary_response(text)
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
