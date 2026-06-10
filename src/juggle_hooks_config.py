"""
juggle_hooks_config — shared constants and DB helpers for all hooks modules.

Owns: DB_PATH, _CHECKPOINT_PATH, AUTOPILOT_FLAG, logging setup, is_active(),
      get_db(), _record_error_safe(), _get_session_id().
Must not own: handler logic, event routing.
"""

import json
import logging
import os
import threading
from pathlib import Path

from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings

_DATA_DIR = Path(_get_settings()["paths"]["data_dir"]).expanduser()
DB_PATH = _DATA_DIR / "juggle.db"

_CHECKPOINT_PATH = _DATA_DIR / "checkpoint.json"
_CHECKPOINT_MAX_AGE_SECS = 3600  # ignore checkpoints older than 1 h

# Flag file written by /juggle:toggle-autopilot. Its presence means autopilot
# mode is ON. Read here so the directive is re-asserted on every prompt — a
# prompt-only toggle would be forgotten on the next turn.
AUTOPILOT_FLAG = Path.home() / ".juggle" / "autopilot"

logging.basicConfig(
    filename=str(_DATA_DIR / "juggle.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def _record_error_safe(exc: Exception, entrypoint: str) -> None:
    """Import record_error lazily to avoid circular import at module load."""
    try:
        from juggle_selfheal import record_error
        record_error(exc, entrypoint)
    except Exception:
        pass  # record_error itself failed; already logged inside


def _db_path():
    """Read DB_PATH at call time so monkeypatches take effect."""
    import sys as _sys
    return _sys.modules[__name__].DB_PATH


def is_active() -> bool:
    """Return True if juggle is enabled and active."""
    p = _db_path()
    if not p.exists():
        return False
    try:
        db = JuggleDB(str(p))
        return db.is_active()
    except Exception as exc:
        logging.warning("is_active check failed: %s", exc)
        return False


def get_db() -> JuggleDB:
    return JuggleDB(str(_db_path()))


def _get_session_id(db) -> str:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    return row["value"] if row else ""
