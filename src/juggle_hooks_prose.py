"""Prose-decision lifecycle for the Stop / UserPromptSubmit hooks.

Mirrors juggle_hooks_askuser for decisions surfaced as plain assistant TEXT
(no tool call fires a hook). On Stop, record_prose_decision files ONE
``[auto-decision]`` action item when the final orchestrator message matches a
decision/advisory cue; on the next UserPromptSubmit, clear_prose_decision
dismisses the most recent one (the user's reply is the answer). Both are
best-effort — a failure here must never break the hook.
"""
from __future__ import annotations

import hashlib
import logging
import re

# Decision/advisory/blocker cues surfaced as plain prose. Broader than the
# Stop-hook's permission-asking nudge set (kept separate in juggle_hooks_prompt):
# these are genuine "the user must choose / give a go" signals, not "you should
# have just acted". Broadened 2026-06-30 (action-item reliability) so prose
# blockers like 'parked for your go' / 'want me to?' / 'needs a call' file too.
_DECISION_ADVISORY_PATTERNS = [
    r"your call",
    r"say ['\"].+?['\"] to proceed",
    r"which (option|one)",
    r"do you want",
    r"let me know",
    r"collides",
    r"want me to",
    r"\bparked\b",
    r"green[\s-]?light",
    r"awaiting your (go|call|decision|approval|sign[\s-]?off)",
    r"needs? (a|your) (call|decision|go|sign[\s-]?off|approval)",
    r"\bshould i\b",
]

_AUTO_DECISION_PREFIX = "[auto-decision]"


def _normalize(text: str) -> str:
    """Collapse whitespace + lowercase for stable dedup comparison."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_hash(text: str) -> str:
    """Stable short content hash of the normalized prose for cross-turn dedup."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()[:12]


def detect_pending_decision(text: str) -> bool:
    """Pure detector (2026-06-30 action-item reliability): True iff the assistant
    text surfaces a user-facing decision / blocker / permission-request cue."""
    if not text:
        return False
    return any(
        re.search(p, text, re.IGNORECASE) for p in _DECISION_ADVISORY_PATTERNS
    )


# Back-compat alias — historical name used inside this module.
is_decision_prose = detect_pending_decision


def record_prose_decision(db, last_msg: str) -> None:
    """File a deduped [auto-decision] action item for a prose decision."""
    try:
        if not detect_pending_decision(last_msg):
            return
        thread_id = db.get_current_thread()
        if not thread_id:
            return

        open_items = db.get_open_action_items()
        # "No action item created this turn": AskUserQuestion's bridge already
        # filed a [tuid:…] item, so the decision is surfaced — don't double-file.
        if any(i.get("message", "").startswith("[tuid:") for i in open_items):
            return
        # Dedup vs self by CONTENT HASH: same decision already open from a prior
        # Stop must not spam a second item (2026-06-30 action-item reliability).
        this_hash = _content_hash(last_msg)
        for i in open_items:
            msg = i.get("message", "")
            if msg.startswith(_AUTO_DECISION_PREFIX):
                body = msg[len(_AUTO_DECISION_PREFIX):]
                if _content_hash(body) == this_hash:
                    return

        db.add_action_item(
            thread_id=thread_id,
            message=f"{_AUTO_DECISION_PREFIX} {last_msg}",
            type_="decision",
            priority="normal",
        )
    except Exception as exc:
        logging.warning("Stop prose-decision handler error: %s", exc)


def clear_prose_decision(db) -> None:
    """Dismiss the most recent open [auto-decision] item (user replied)."""
    try:
        auto = [
            i
            for i in db.get_open_action_items()
            if i.get("message", "").startswith(_AUTO_DECISION_PREFIX)
        ]
        if auto:
            db.dismiss_action_item(max(auto, key=lambda i: i["id"])["id"])
    except Exception as exc:
        logging.warning("UserPromptSubmit prose-decision clear error: %s", exc)
