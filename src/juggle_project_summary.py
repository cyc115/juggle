"""Project summarizer — produces per-thread and overall project summaries via Claude."""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def _claude_sonnet(prompt: str) -> str:
    """Call `claude -p <prompt> --model sonnet`. Injectable for tests.

    Thin wrapper over llm_calls.run_claude_p (single source of truth);
    returns "" on non-zero exit or any exception.
    """
    from llm_calls import run_claude_p

    try:
        out = run_claude_p(prompt, model="sonnet", timeout=120, log=_log)
        return "" if out is None else out
    except Exception as e:
        _log.warning("_claude_sonnet error: %s", e)
        return ""


def summarize_project(
    db,
    project_id: str,
    llm_fn=None,
) -> tuple[str, dict[str, str]]:
    """Summarize all threads and synthesize an overall project summary.

    Returns (project_summary, {thread_id: thread_summary}).
    llm_fn(prompt) -> str is injectable so tests can mock it.
    """
    if llm_fn is None:
        llm_fn = _claude_sonnet

    project = db.get_project(project_id)
    threads = db.get_threads_by_project(project_id)

    thread_summaries: dict[str, str] = {}
    for thread in threads:
        tid = thread["id"]
        messages = db.get_messages(tid, token_budget=4000)
        msg_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:500]}" for m in messages[-20:]
        )
        prompt = (
            "Summarize this conversation thread in 2-3 sentences. "
            "Cover: what was discussed, what was decided, current state.\n\n"
            f"Topic: {thread['title']}\n"
            f"Messages:\n{msg_text or '(no messages)'}"
        )
        thread_summaries[tid] = llm_fn(prompt)

    if threads:
        thread_lines = "\n".join(
            f"- {t['title']}: {thread_summaries[t['id']]}"
            for t in threads
            if t["id"] in thread_summaries
        )
        proj_prompt = (
            "Write a 2-3 sentence overall project summary (what was accomplished, current state).\n"
            f"Project: {project['name']}\n"
            f"Objective: {project.get('objective', '')}\n"
            f"Topics:\n{thread_lines}"
        )
    else:
        proj_prompt = (
            "Summarize this project in 1-2 sentences based on its definition.\n"
            f"Project: {project['name']}\n"
            f"Objective: {project.get('objective', '')}"
        )

    project_summary = llm_fn(proj_prompt)
    return project_summary, thread_summaries
