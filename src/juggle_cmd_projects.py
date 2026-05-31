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


def assign_project_background(
    db,
    thread_uuid: str,
    topic: str,
    _return_thread: bool = False,
) -> threading.Thread | None:
    """Fire-and-forget background project assignment.

    Failure contract: all exceptions caught and logged only. Thread stays INBOX.
    Never raises, never blocks, no user-visible side-effects on failure.
    _return_thread=True for testing only — returns Thread so caller can join.
    """
    def _run():
        try:
            projects = db.get_active_projects()
            project_id = infer_project_id(topic, projects)
            if project_id != INBOX_PROJECT_ID:
                db.update_thread(thread_uuid, project_id=project_id)
                log.info("assign_project_background: %s -> %s", thread_uuid[:8], project_id)
        except Exception as e:
            log.warning("assign_project_background: silent failure: %s", e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t if _return_thread else None


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
