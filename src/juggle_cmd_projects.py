"""Juggle project management — CLI commands and background assignment."""
from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_cli_common import _cheap_llm_call

INBOX_PROJECT_ID = "INBOX"
log = logging.getLogger(__name__)


def infer_project_id(topic: str, projects: list[dict]) -> str:
    """Pure function — returns best project_id or INBOX. No DB, no threads, no side-effects."""
    if not projects:
        return INBOX_PROJECT_ID
    valid_ids = {p["id"] for p in projects} | {INBOX_PROJECT_ID}
    project_list = "; ".join(f'{p["id"]}: {p["name"]} — {p["objective"]}' for p in projects)
    prompt = (
        f'Topic: "{topic}". '
        f'Projects: [{project_list}]. '
        f'Which project fits best? Return JSON only: {{"project_id": "<id_or_INBOX>"}}. No explanation.'
    )
    raw = _cheap_llm_call(prompt, timeout=5)
    if not raw:
        return INBOX_PROJECT_ID
    try:
        pid = json.loads(raw).get("project_id", INBOX_PROJECT_ID)
        return pid if pid in valid_ids else INBOX_PROJECT_ID
    except (json.JSONDecodeError, AttributeError):
        log.warning("infer_project_id: unparseable response: %r", raw)
        return INBOX_PROJECT_ID
