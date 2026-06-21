"""dbops.schema — DDL constants, module-level helpers, and shared config.

Owns: all CREATE TABLE/INDEX strings, the INBOX sentinel, message-truncation
constants, and pure helper functions that the mixin layer depends on.
Must not own: any JuggleDB method, any connection logic, any business logic.
"""

from __future__ import annotations

import logging
import os
import string
from datetime import datetime, timezone
from pathlib import Path

from juggle_settings import get_settings as _get_settings  # noqa: E402

_log = logging.getLogger(__name__)

MAX_THREADS: int = _get_settings()["max_threads"]
MAX_BACKGROUND_AGENTS: int = _get_settings()["max_agents"]

DEFAULT_DATA_DIR = Path(_get_settings()["paths"]["data_dir"])

# DB path override: `JUGGLE_DB_PATH` lets tests (and any out-of-tree run) point
# at an isolated DB so they can NEVER pollute the production DB. Default is
# unchanged when the env var is unset (2026-06-16 isolation incident fix).
JUGGLE_DB_PATH_ENV = "JUGGLE_DB_PATH"


def _resolve_db_path() -> Path:
    """Resolve the DB path, honoring `JUGGLE_DB_PATH`; default unchanged."""
    env = os.environ.get(JUGGLE_DB_PATH_ENV)
    if env:
        return Path(env).expanduser()
    return DEFAULT_DATA_DIR / "juggle.db"


DB_PATH = _resolve_db_path()

INBOX_PROJECT_ID = "INBOX"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

CREATE_THREADS = """
CREATE TABLE IF NOT EXISTS threads (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL DEFAULT '',
  topic           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  summary         TEXT DEFAULT '',
  key_decisions   TEXT DEFAULT '[]',
  open_questions  TEXT DEFAULT '[]',
  last_user_intent TEXT DEFAULT '',
  agent_task_id   TEXT,
  agent_result    TEXT,
  show_in_list    INTEGER NOT NULL DEFAULT 1,
  summarized_msg_count INTEGER NOT NULL DEFAULT 0,
  title           TEXT DEFAULT '',
  created_at      TEXT NOT NULL,
  last_active     TEXT NOT NULL,
  last_dispatched_task  TEXT,
  last_dispatched_role  TEXT,
  last_dispatched_model TEXT,
  worktree_path         TEXT,
  worktree_branch       TEXT,
  main_repo_path        TEXT
);
"""

CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT NOT NULL REFERENCES threads(id),
  role            TEXT NOT NULL,
  content         TEXT NOT NULL,
  token_estimate  INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL
);
"""

CREATE_NOTIFICATIONS = """
CREATE TABLE IF NOT EXISTS notifications (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT NOT NULL REFERENCES threads(id),
  message         TEXT NOT NULL,
  delivered       INTEGER DEFAULT 0,
  created_at      TEXT NOT NULL
);
"""

CREATE_SESSION = """
CREATE TABLE IF NOT EXISTS session (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);
"""

CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
  id                         TEXT PRIMARY KEY,
  role                       TEXT NOT NULL,
  pane_id                    TEXT NOT NULL,
  assigned_thread            TEXT,
  status                     TEXT NOT NULL DEFAULT 'idle',
  context_threads            TEXT NOT NULL DEFAULT '[]',
  created_at                 TEXT NOT NULL,
  last_active                TEXT NOT NULL,
  watchdog_retried           INTEGER NOT NULL DEFAULT 0,
  watchdog_threshold_minutes INTEGER,
  model                      TEXT,
  last_task                  TEXT,
  busy_since                 TEXT,
  last_send_task_pane_hash   TEXT,
  last_send_task_at          TEXT,
  last_activity_at           TEXT,
  repo_path                  TEXT
);
"""

CREATE_NOTIFICATIONS_V2 = """
CREATE TABLE IF NOT EXISTS notifications_v2 (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT,
  message         TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  session_id      TEXT NOT NULL,
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL
);
"""

CREATE_ACTION_ITEMS = """
CREATE TABLE IF NOT EXISTS action_items (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id       TEXT,
  message         TEXT NOT NULL,
  type            TEXT NOT NULL,
  priority        TEXT NOT NULL DEFAULT 'normal',
  created_at      TEXT NOT NULL,
  dismissed_at    TEXT,
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE SET NULL
);
"""

CREATE_SETTINGS = """
CREATE TABLE IF NOT EXISTS settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);
"""

CREATE_AGENT_COMPLETIONS = """
CREATE TABLE IF NOT EXISTS agent_completions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role          TEXT NOT NULL,
  duration_secs REAL NOT NULL,
  completed_at  TEXT NOT NULL
);
"""

CREATE_WATCHDOG_EVENTS = """
CREATE TABLE IF NOT EXISTS watchdog_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id      TEXT NOT NULL,
  thread_id     TEXT,
  event_type    TEXT NOT NULL,
  snapshot_path TEXT,
  created_at    TEXT NOT NULL
);
"""

# Per-agent tool-usage telemetry. Aggregated by (role, tool, mode) so the table
# stays tiny regardless of volume: each tool call increments `count` rather than
# inserting a row. `mode` distinguishes steady-state usage ('normal') from
# audit-mode runs ('audit', per-role denies relaxed) so the report can tell
# "what a role uses" from "what a role would use if not blocked".
CREATE_AGENT_TOOL_EVENTS = """
CREATE TABLE IF NOT EXISTS agent_tool_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  role        TEXT NOT NULL,
  tool_name   TEXT NOT NULL,
  mode        TEXT NOT NULL DEFAULT 'normal',
  count       INTEGER NOT NULL DEFAULT 1,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL,
  last_input  TEXT,
  UNIQUE(role, tool_name, mode)
);
"""


# Self-heal DDL + status vocabulary live in dbops.schema_selfheal (architecture
# gate). Re-exported so ``from dbops.schema import CREATE_ERROR_EVENTS`` etc. keep
# working.
from dbops.schema_selfheal import (  # noqa: E402,F401
    CREATE_ERROR_EVENTS,
    CREATE_SELFHEAL_AUDIT,
    VALID_ERROR_STATUSES,
)

CREATE_PROJECTS = """
CREATE TABLE IF NOT EXISTS projects (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL,
  objective        TEXT NOT NULL DEFAULT '',
  success_criteria TEXT NOT NULL DEFAULT '[]',
  out_of_scope     TEXT DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',
  summary          TEXT DEFAULT '',
  closed_at        TEXT,
  created_at       TEXT NOT NULL,
  last_active      TEXT NOT NULL,
  match_profile    TEXT DEFAULT '',
  profile_synth_at TEXT,
  profile_dirty    INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_PROJECT_CORRECTIONS = """
CREATE TABLE IF NOT EXISTS project_corrections (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  topic        TEXT NOT NULL,
  from_project TEXT NOT NULL,
  to_project   TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
"""

# Autopilot graph/topic store DDL lives in dbops.schema_graph (architecture
# gate: ≤300 lines/module). Re-exported here so existing
# ``from dbops.schema import CREATE_GRAPH_TASKS`` imports keep working.
from dbops.schema_graph import (  # noqa: E402,F401
    CREATE_GRAPH_EDGES,
    CREATE_GRAPH_TASKS,
    CREATE_GRAPH_TOPICS,
)

# agent_runs ledger DDL lives in dbops.schema_runs (architecture gate). Re-
# exported here so ``from dbops.schema import CREATE_AGENT_RUNS`` keeps working.
from dbops.schema_runs import (  # noqa: E402,F401
    CREATE_AGENT_RUNS,
    CREATE_AGENT_RUNS_INDEXES,
)

# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Topic Slug Wheel (T-slug-wheel): reusable rotating two-letter handles.
# ---------------------------------------------------------------------------
WHEEL_SIZE = 676  # 26 * 26 two-letter slots (AA..ZZ)


def _slug_from_wheel(seq: int) -> str:
    """Map a wheel position to its two-letter slug.

    i=0 -> 'AA', i=1 -> 'AB', ..., i=675 -> 'ZZ', then wraps to 'AA'.
    """
    i = seq % WHEEL_SIZE
    return chr(ord("A") + i // 26) + chr(ord("A") + i % 26)


def _wheel_index(slug: str | None) -> int | None:
    """Inverse of _slug_from_wheel for a valid two-letter A-Z slug, else None."""
    if not slug or len(slug) != 2 or not slug.isalpha():
        return None
    a, b = slug.upper()
    return (ord(a) - ord("A")) * 26 + (ord(b) - ord("A"))


def _thread_age_seconds(last_active: str | None) -> float | None:
    """Parse last_active ISO timestamp, return seconds since now, or None."""
    if not last_active:
        return None
    try:
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


def _is_junk_message(content: str) -> bool:
    """Return True if content is a junk/system message to be excluded from display."""
    return (
        content.startswith("<task-notification")
        or "</task-notification>" in content
        or "task-id" in content
        or "<tool_uses>" in content
        or content.strip().startswith("/")
    )


# Structural markers that identify orchestrator-generated chatter: autopilot
# status cards, JUGGLE ACTIVE context blocks, loop-tick headers, and status
# dump section headings.  Content containing any of these is never a legitimate
# user topic and must not spawn a new thread.
_ORCHESTRATOR_CHATTER_MARKERS: tuple[str, ...] = (
    "--- AUTOPILOT MODE:",          # autopilot directive header
    "# Autonomous loop tick",        # orchestrator loop-tick output
    "--- JUGGLE ACTIVE",             # JUGGLE ACTIVE context block marker
    "# Active Threads",              # status dump section heading
    "# Notifications (this session)",  # status dump section heading
    "--- END JUGGLE ---",            # context block footer
)


def is_auto_topic_eligible(content: str) -> bool:
    """Return True if content is a legitimate topic for automatic thread creation.

    Returns False for orchestrator-generated chatter (autopilot status cards,
    JUGGLE ACTIVE blocks, loop-tick headers) so those never spawn junk threads.
    """
    return not any(marker in content for marker in _ORCHESTRATOR_CHATTER_MARKERS)
