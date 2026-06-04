"""Project summarizer — produces per-thread and overall project summaries via Claude."""
from __future__ import annotations

import logging
import subprocess

_log = logging.getLogger(__name__)


def _claude_sonnet(prompt: str) -> str:
    """Call `claude -p <prompt> --model sonnet`. Injectable for tests."""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "sonnet"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            _log.warning("_claude_sonnet failed: %s", result.stderr[:200])
            return ""
        return result.stdout.strip()
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
            f"Topic: {thread.get('title') or thread['topic']}\n"
            f"Messages:\n{msg_text or '(no messages)'}"
        )
        thread_summaries[tid] = llm_fn(prompt)

    if threads:
        thread_lines = "\n".join(
            f"- {t.get('title') or t['topic']}: {thread_summaries[t['id']]}"
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
