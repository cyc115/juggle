#!/usr/bin/env python3
"""Juggle CLI Common - shared constants, DB access, and utility functions."""

import logging
import os
import subprocess  # noqa: F401  — kept: tests patch juggle_cli_common.subprocess.run
import sys
from datetime import datetime, timezone
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))
from juggle_db import DB_PATH as _DEFAULT_DB_PATH  # noqa: E402
from juggle_settings import get_settings as _get_settings  # noqa: E402
# Re-export shim: llm_call lives in llm_calls.py (single `claude -p` source of
# truth). Kept as a module attribute so patch("juggle_cli_common.llm_call")
# call sites keep working.
from llm_calls import llm_call  # noqa: E402,F401

DB_PATH = (
    Path(os.environ["_JUGGLE_TEST_DB"])
    if "_JUGGLE_TEST_DB" in os.environ
    else _DEFAULT_DB_PATH
)

JUGGLE_CONFIG_PATH = Path(_get_settings()["paths"]["config_dir"]) / "config.json"


def _get_hindsight_client():
    """Return HindsightClient or None if disabled/unconfigured."""
    from juggle_hindsight import HindsightClient

    return HindsightClient.from_config()


def get_db(db_path=None, init=False):
    """Return a JuggleDB handle.

    db_path: optional override. When omitted, falls back to `_JUGGLE_TEST_DB`
             (module DB_PATH) if set, else lets `JuggleDB()` resolve the path —
             which honors `JUGGLE_DB_PATH` so test isolation can never land on
             the production DB. init: call init_db() before returning.
    """
    from juggle_db import JuggleDB

    if db_path:
        db = JuggleDB(str(db_path))
    elif "_JUGGLE_TEST_DB" in os.environ:
        db = JuggleDB(str(DB_PATH))
    else:
        db = JuggleDB()  # honors JUGGLE_DB_PATH (isolation) / prod default
    if init:
        db.init_db()
    return db


def _resolve_thread(db, thread_id_input: str) -> str:
    """Resolve user-label or hex-prefix/full UUID to thread UUID.

    Accepts:
      - 1-2 letter user label (A..Z, AA..ZZ) — case-insensitive
      - Full 36-char UUID
      - 6+ char hex prefix
    """
    s = (thread_id_input or "").strip()
    if not s:
        print("Error: empty thread id")
        sys.exit(1)

    # User-label path (1-2 uppercase letters)
    if 1 <= len(s) <= 2 and s.isalpha():
        t = db.get_thread_by_user_label(s.upper())
        if t:
            return t["id"]
        print(f"Error: no thread with label {s.upper()}")
        sys.exit(1)

    # Full UUID
    if len(s) == 36 and s.count("-") == 4:
        t = db.get_thread(s)
        if t:
            return s
        print(f"Error: no thread with id {s}")
        sys.exit(1)

    # Hex prefix (6+ chars, all hex digits)
    if all(c in "0123456789abcdef-" for c in s.lower()) and len(s) >= 6:
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM threads WHERE id LIKE ?", (s.lower() + "%",)
            ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            print(f"Error: ambiguous prefix {s}; matches {len(rows)} threads")
            sys.exit(1)
        print(f"Error: no thread matching prefix {s}")
        sys.exit(1)

    print(f"Error: unrecognised thread id format: {s!r}")
    sys.exit(1)


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


def _cheap_llm_call(prompt: str, timeout: int = 10) -> str | None:
    """Shim: delegates to llm_call(profile='cheap'). Kept for call-site compat."""
    return llm_call(prompt, profile="cheap", timeout=timeout)


def _generate_title_for_thread(db, thread_uuid: str, topic: str) -> str:
    """Generate a 5-10 word title. Fallback chain: OpenRouter → Haiku → first 5 words."""
    from juggle_settings import get_settings

    cfg = get_settings().get("title_gen", {})
    fallback = " ".join(topic.replace("-", " ").replace("_", " ").split()[:5]).title()
    prompt = (
        f'Convert this task identifier into a concise 4-8 word descriptive title in Title Case. '
        f'Task: "{topic}". Reply with the title only. No punctuation. No quotes. No explanation. Use Title Case.'
    )
    timeout = cfg.get("timeout_secs", 10)

    def _valid(text: str) -> bool:
        if not text:
            return False
        words = text.split()
        return 3 <= len(words) <= 15 and "-" not in text and not all(w.islower() for w in words)

    title = _cheap_llm_call(prompt, timeout=timeout)
    if title and _valid(title):
        if not any(c.isupper() for c in title):
            title = title.title()
        logging.info("_generate_title_for_thread: -> %r", title)
        db.update_thread(thread_uuid, title=title)
        return title
    logging.info("_generate_title_for_thread: fallback -> %r", fallback)
    db.update_thread(thread_uuid, title=fallback)
    return fallback
