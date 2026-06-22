"""AskUserQuestion decision lifecycle for the PreToolUse / PostToolUse hooks.

Extracted verbatim from juggle_hooks_tooluse.py to keep that module within its
LOC budget (architecture gate). PreToolUse records each pending decision (open
question + a decision action item); PostToolUse clears them once the question is
answered. Both are best-effort — a failure here must never break the hook.
"""
from __future__ import annotations

import json
import logging


def record_askuser_decision(db, data: dict) -> None:
    """Record pending AskUserQuestion decisions on the current thread."""
    try:
        thread_id = db.get_current_thread()
        if thread_id:
            tool_use_id = data.get("tool_use_id", "")
            questions = data.get("tool_input", {}).get("questions", [])

            thread = db.get_thread(thread_id)
            current = thread.get("open_questions") or []
            if isinstance(current, str):
                current = json.loads(current)

            for i, q in enumerate(questions):
                current.append(
                    {
                        "id": f"{tool_use_id}:{i}",
                        "text": q.get("question", ""),
                        "source": "askuser",
                    }
                )

            db.update_thread(thread_id, open_questions=current)

            question_text = " / ".join(q.get("question", "") for q in questions)
            db.add_action_item(
                thread_id=thread_id,
                message=f"[tuid:{tool_use_id}] Decision needed: {question_text}",
                type_="decision",
                priority="normal",
            )
    except Exception as exc:
        logging.warning("AskUserQuestion PreToolUse handler error: %s", exc)


def clear_askuser_decision(db, data: dict) -> None:
    """Clear pending decisions once an AskUserQuestion completes."""
    try:
        thread_id = db.get_current_thread()
        if thread_id:
            tool_use_id = data.get("tool_use_id", "")

            thread = db.get_thread(thread_id)
            open_questions = thread.get("open_questions") or []
            if isinstance(open_questions, str):
                open_questions = json.loads(open_questions)

            open_questions = [
                q
                for q in open_questions
                if not q.get("id", "").startswith(tool_use_id)
            ]

            db.update_thread(thread_id, open_questions=open_questions)

            prefix = f"[tuid:{tool_use_id}]"
            open_items = db.get_open_action_items()
            for item in open_items:
                if item.get("message", "").startswith(prefix):
                    db.dismiss_action_item(item["id"])
    except Exception as exc:
        logging.warning("AskUserQuestion PostToolUse handler error: %s", exc)
