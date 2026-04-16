#!/usr/bin/env python3
"""Juggle CLI Common - shared constants, DB access, and utility functions."""

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))
from juggle_db import DB_PATH as _DEFAULT_DB_PATH  # noqa: E402
from juggle_settings import get_settings as _get_settings  # noqa: E402

DB_PATH = Path(os.environ["_JUGGLE_TEST_DB"]) if "_JUGGLE_TEST_DB" in os.environ else _DEFAULT_DB_PATH

# Env var already folded into get_settings(); keep constant for importers (juggle_cmd_agents etc.)
JUGGLE_IDLE_THRESHOLD_SECS: int = _get_settings()["tmux"]["agent_idle_detection_secs"]

JUGGLE_CONFIG_PATH = Path(_get_settings()["paths"]["config_dir"]) / "config.json"


def _get_hindsight_client():
    """Return HindsightClient or None if disabled/unconfigured."""
    from juggle_hindsight import HindsightClient
    return HindsightClient.from_config(str(JUGGLE_CONFIG_PATH))


def get_db():
    from juggle_db import JuggleDB
    return JuggleDB(str(DB_PATH))


def _resolve_thread(db, label_or_id: str) -> str:
    """Accept label (e.g. 'A') or UUID. Return UUID.

    Raises SystemExit(1) if the label is not found.
    """
    if len(label_or_id) == 1 and label_or_id.isalpha():
        thread = db.get_thread_by_label(label_or_id.upper())
        if not thread:
            print(f"Error: No active thread with label '{label_or_id.upper()}'.")
            sys.exit(1)
        return thread["id"]
    return label_or_id  # already a UUID


def _humanize_dt(iso_str: str) -> str:
    """Return a human-friendly relative time string for an ISO-8601 UTC timestamp."""
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return "just now"
        if diff < 3600:
            mins = int(diff // 60)
            return f"{mins} min ago"
        if diff < 86400:
            hrs = int(diff // 3600)
            return f"{hrs} hr ago"
        if diff < 172800:
            return "yesterday"
        days = int(diff // 86400)
        if days < 7:
            return f"{days} days ago"
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str


def _last_sentences(text: str, max_chars: int = 200) -> str:
    """Return the tail of text, capped at max_chars."""
    return text.strip()[:max_chars] if text else ""


def _extract_decision_prompt(last_assistant: str | None, last_user: str | None) -> str:
    """Return a concise actionable prompt for a ⏸️ waiting thread.

    Extracts the last question from the assistant's message, or falls back
    to showing the unanswered user message.
    """
    import re as _re

    if last_assistant and "?" in last_assistant:
        sentences = _re.split(r"(?<=[.!?])\s+", last_assistant.strip())
        questions = [s.strip() for s in sentences if "?" in s and len(s.strip()) > 5]
        if questions:
            q = _re.sub(r"\*+", "", questions[-1]).strip()
            if len(q) > 80:
                q = q[:77] + "..."
            return f"🤔 {q}"

    if last_user:
        msg = last_user.strip()
        if len(msg) > 60:
            msg = msg[:57] + "..."
        return f'📬 Respond to: "{msg}"'

    return "🤔 Waiting for input"


def _generate_title_for_thread(db, thread_uuid: str, topic: str) -> str:
    """Generate a 5-10 word title for a thread via claude -p. Stores result in DB.

    Falls back to first 5 words of topic if claude is unavailable or returns garbage.
    Returns the title string.
    """
    fallback = " ".join(topic.split()[:5])
    prompt = (
        f'Give a 5-10 word title for this task: "{topic}". '
        f'Reply with the title only. No punctuation. No quotes. No explanation.'
    )
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=20
        )
        title = result.stdout.strip()
        # Sanity check: non-empty and not excessively long
        if not title or len(title.split()) > 15:
            title = fallback
    except Exception:
        title = fallback
    db.update_thread(thread_uuid, title=title)
    return title
