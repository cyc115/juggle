"""dbops.threads — Thread CRUD, state machine, archive, and stale-query mixin.

Owns: create/get/update/list threads, thread status transitions, archive/
unarchive, stale-thread detection, and archive-candidate selection.
Must not own: message content, project assignment, agent pool, notifications.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

import dbops.schema as _schema
from dbops.schema import (
    WHEEL_SIZE,
    _get_settings,
    _is_junk_message,
    _slug_from_wheel,
    _thread_age_seconds,
)

# Read MAX_THREADS via module reference so tests can patch dbops.threads.MAX_THREADS
# (or dbops.schema.MAX_THREADS) to bypass the cap in seeding fixtures.
MAX_THREADS = _schema.MAX_THREADS

# ---------------------------------------------------------------------------
# Lexical thread-dedup guard (v1 — deterministic, NO LLM)
#
# A new thread whose title is a strong lexical match of an OPEN same-project
# thread is a semantic duplicate; create_thread reuses the existing thread
# instead of spawning a twin. This single chokepoint covers both origins of
# thread creation: manual `create-thread` and the graph-tick dispatch path.
#
# _title_similarity is kept PURE and isolated so a future semantic/embedding
# scorer can replace it without touching the call sites.
# ---------------------------------------------------------------------------

# Reuse threshold on the 0..1 similarity score. >= this is a duplicate.
THREAD_DEDUP_THRESHOLD = 0.8

# Statuses considered OPEN (live work). Closed/archived threads are historical
# and are NEVER reuse targets.
_OPEN_THREAD_STATES = ("active", "running", "background")

# Leading "[T-<id>] " graph-topic prefix stamped onto dispatch-thread titles.
_TOPIC_PREFIX_RE = re.compile(r"^\s*\[t-[^\]]+\]\s*", re.IGNORECASE)

# Function words and structural filler that carry no topical signal.
_DEDUP_STOPWORDS = frozenset({
    "a", "an", "the", "of", "for", "to", "and", "or", "in", "on", "with",
    "via", "by", "at", "is", "are", "was", "were", "be", "this", "that",
    "it", "as", "from", "into", "topic",
})


def _normalize_title_tokens(title: str) -> set[str]:
    """Lowercase, strip a leading graph-topic prefix, drop punctuation and
    stopwords, and tokenize to a set of significant tokens."""
    s = (title or "").lower()
    s = _TOPIC_PREFIX_RE.sub("", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return {tok for tok in s.split() if tok and tok not in _DEDUP_STOPWORDS}


def _title_similarity(a: str, b: str) -> float:
    """Lexical title similarity in 0..1.

    Score = max(token-set containment |A∩B|/min(|A|,|B|), Jaccard
    |A∩B|/|A∪B|). Containment catches the case where one title is a terse
    subset of a longer one (e.g. "slug wheel" vs the full dispatch title).
    """
    ta = _normalize_title_tokens(a)
    tb = _normalize_title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if inter == 0:
        return 0.0
    containment = inter / min(len(ta), len(tb))
    jaccard = inter / len(ta | tb)
    return max(containment, jaccard)


class ThreadsMixin:
    """Mixin for thread CRUD, state machine, archive ops, and stale detection."""

    # ---------------------------------------------------------------
    # Thread CRUD
    # ---------------------------------------------------------------

    def _find_duplicate_open_thread(
        self, topic: str, project_id: str | None
    ) -> str | None:
        """Return the id of an OPEN thread whose title is a lexical duplicate of
        `topic`, or None. Scoped to `project_id` when known, else global.

        Safety: only OPEN threads are eligible, and a thread that already OWNS a
        graph topic or task is excluded — those are real in-flight work and must
        never be collapsed into another topic.
        """
        with self._connect() as conn:
            if project_id is not None:
                rows = conn.execute(
                    "SELECT id, topic, title FROM threads "
                    f"WHERE status IN ({','.join('?' * len(_OPEN_THREAD_STATES))}) "
                    "AND project_id = ?",
                    (*_OPEN_THREAD_STATES, project_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, topic, title FROM threads "
                    f"WHERE status IN ({','.join('?' * len(_OPEN_THREAD_STATES))})",
                    _OPEN_THREAD_STATES,
                ).fetchall()
            owned: set[str] = set()
            for tbl in ("graph_topics", "graph_tasks"):
                try:
                    owned.update(
                        r["thread_id"]
                        for r in conn.execute(
                            f"SELECT thread_id FROM {tbl} WHERE thread_id IS NOT NULL"
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    pass  # graph tables absent on a pre-autopilot DB
        for row in rows:
            if row["id"] in owned:
                continue
            candidate = row["title"] or row["topic"] or ""
            if _title_similarity(topic, candidate) >= THREAD_DEDUP_THRESHOLD:
                return row["id"]
        return None

    def create_thread(
        self, topic: str, session_id: str, project_id: str | None = None
    ) -> str:
        """Create a new thread. Returns the UUID of the new thread.

        Assigns next available A–Z label. Raises ValueError if 10 non-archived
        threads already exist or all 26 labels are in use.

        Dedup guard: if an OPEN (same-project, when `project_id` is given)
        thread already exists whose title is a lexical duplicate of `topic`,
        no new row is inserted and that existing thread's id is returned.
        """
        existing = self._find_duplicate_open_thread(topic, project_id)
        if existing is not None:
            return existing
        with self._connect() as conn:
            rows = conn.execute("SELECT id, status FROM threads").fetchall()
            active_count = sum(1 for row in rows if row["status"] != "archived")
            if active_count >= MAX_THREADS:  # noqa: SIM102 — patched by test fixtures
                candidates = self.get_archive_candidates()
                if candidates:
                    cmds = ", ".join(
                        f"[{t.get('user_label') or t.get('label')}] "
                        f"{(t.get('title') or t.get('topic') or '')[:40]}"
                        f" → archive-thread {t.get('user_label') or t.get('label')}"
                        for t in candidates[:5]
                    )
                    raise ValueError(
                        f"Maximum of {MAX_THREADS} threads already exist. "
                        f"Archivable: {cmds}"
                    )
                else:
                    raise ValueError(
                        f"Maximum of {MAX_THREADS} threads already exist. "
                        "No immediate candidates — close or archive a thread manually."
                    )
            new_id = str(uuid.uuid4())
            now_iso = datetime.now(timezone.utc).isoformat()
            now_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            # Allocate a slug from the rotating wheel (skip-live). The retry loop
            # is a backstop: if a live holder raced onto the slug between alloc
            # and INSERT, the partial unique index rejects it and we advance the
            # wheel for another attempt.
            for _attempt in range(3):
                user_label = self._next_wheel_slug(conn)
                try:
                    conn.execute(
                        """
                        INSERT INTO threads
                          (id, user_label, session_id, topic, status,
                           key_decisions, open_questions,
                           last_user_intent, agent_task_id, agent_result,
                           show_in_list, summarized_msg_count, created_at, last_active, last_active_at)
                        VALUES (?, ?, ?, ?, 'active', '[]', '[]', '', NULL, NULL, 1, 0, ?, ?, ?)
                        """,
                        (new_id, user_label, session_id, topic, now_iso, now_iso, now_min),
                    )
                    conn.commit()
                    return new_id
                except sqlite3.IntegrityError as exc:
                    if "user_label" not in str(exc) and "idx_threads_live_label" not in str(exc):
                        raise
                    # A live holder raced onto this slug; loop advances the wheel.
                    continue
            raise RuntimeError(
                "create_thread: could not assign a slug after retries"
            )

    def _next_wheel_slug(self, conn) -> str:
        """Allocate the next slug off the wheel, skipping live-held slots.

        Reads + increments the durable monotonic counter ``label_seq`` in
        juggle_meta inside the caller's write transaction (SQLite serializes
        writers). Advances past any slug currently held by a live thread
        ('active'/'running'). Raises RuntimeError if the entire wheel is live.
        """
        row = conn.execute(
            "SELECT value FROM juggle_meta WHERE key = 'label_seq'"
        ).fetchone()
        seq = int(row["value"]) if row and row["value"] is not None else 0
        live = {
            r["user_label"]
            for r in conn.execute(
                "SELECT user_label FROM threads WHERE user_label IS NOT NULL "
                "AND status IN ('active','running')"
            ).fetchall()
        }
        for _ in range(WHEEL_SIZE):
            slug = _slug_from_wheel(seq)
            seq += 1
            if slug not in live:
                conn.execute(
                    "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(seq),),
                )
                return slug
        raise RuntimeError(
            "slug wheel exhausted: all 676 slots held by live threads"
        )

    def get_thread(self, thread_id: str) -> dict | None:
        """Look up a thread by its UUID `id`. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_thread_by_user_label(self, label: str | None) -> dict | None:
        """Resolve a user-typed slug to a thread — the SINGLE chokepoint.

        Newest-wins (T-slug-wheel): since slugs rotate and persist on closed/
        archived rows, a reused slug always resolves to the NEWEST holder —
        a live ('active'/'running') holder first, then the most recently
        created terminal holder. Case-insensitive. Returns None if not found.

        Every feature that maps a user-typed slug -> thread MUST route through
        this function so reuse resolves consistently everywhere.
        """
        if not label:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM threads WHERE user_label = ? COLLATE NOCASE "
                "ORDER BY (CASE WHEN status IN ('active','running') THEN 0 ELSE 1 END), "
                "created_at DESC "
                "LIMIT 1",
                (label,),
            ).fetchone()
        return dict(row) if row else None

    def get_all_threads(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM threads ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def update_thread(self, thread_id: str, **kwargs):
        """Update any column(s) on a thread row."""
        import json

        if not kwargs:
            return
        # T-slug-wheel: the slug PERSISTS on close/archive as a permanent
        # historical handle — never null it here (no recycling-by-erasure).
        # Serialize list values to JSON
        for key, val in kwargs.items():
            if isinstance(val, list):
                kwargs[key] = json.dumps(val)
        set_clause = ", ".join(f"{col} = ?" for col in kwargs)
        values = list(kwargs.values()) + [thread_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE threads SET {set_clause} WHERE id = ?",
                values,
            )
            conn.commit()

    # ---------------------------------------------------------------
    # Thread state machine
    # ---------------------------------------------------------------

    _VALID_STATES = {"active", "running", "closed", "archived"}

    def set_thread_status(self, thread_id: str, status: str) -> None:
        """Transition a thread to a new state ({'active','running','closed','archived'}).

        Updates last_active_at to now (UTC, minute precision).
        Raises ValueError for any other status value.
        """
        if status not in self._VALID_STATES:
            raise ValueError(
                f"invalid status {status!r}; must be one of {sorted(self._VALID_STATES)}"
            )
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            # T-slug-wheel: the slug stays on the row through any terminal
            # transition — it is a permanent historical handle.
            conn.execute(
                "UPDATE threads SET status = ?, last_active_at = ? WHERE id = ?",
                (status, now, thread_id),
            )
            conn.commit()

    def touch_last_active(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def get_threads_by_status(self, status: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE status = ? ORDER BY last_active DESC",
                (status,),
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------------------------------------------------------------
    # Archive operations
    # ---------------------------------------------------------------

    def archive_thread(self, thread_id: str):
        """Set status='archived', show_in_list=0.

        T-slug-wheel: keeps user_label as a permanent historical handle (no
        recycling-by-erasure). The slug becomes reusable by a newer thread via
        the wheel's skip-live rule, not by nulling this row."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.execute(
                "UPDATE threads SET status = 'archived', "
                "show_in_list = 0, last_active_at = ? WHERE id = ?",
                (now, thread_id),
            )
            conn.commit()

    def unarchive_thread(self, thread_id: str) -> str:
        """Unarchive: status=active, show_in_list=1.

        T-slug-wheel: the archived row kept its slug, so reuse it when no LIVE
        thread currently holds it; otherwise allocate a fresh slug off the wheel
        to satisfy the partial unique 'no two live share a slug' invariant."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT user_label FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            existing = cur["user_label"] if cur else None
            live = {
                row["user_label"]
                for row in conn.execute(
                    "SELECT user_label FROM threads WHERE user_label IS NOT NULL"
                    " AND status IN ('active','running') AND id != ?",
                    (thread_id,),
                ).fetchall()
            }
            new_label = (
                existing
                if existing and existing not in live
                else self._next_wheel_slug(conn)
            )
            conn.execute(
                "UPDATE threads SET status = 'active', show_in_list = 1, "
                "user_label = ?, last_active_at = ? WHERE id = ?",
                (new_label, now, thread_id),
            )
            conn.commit()
        return new_label

    # ---------------------------------------------------------------
    # Stale / archive-candidate queries
    # ---------------------------------------------------------------

    def get_stale_threads(self, threshold: int | None = None) -> list[dict]:
        """Return threads where substantive user message delta >= threshold.

        Uses a single DB query for all threads instead of N per-thread calls.
        """
        limit: int = (
            threshold
            if threshold is not None
            else int(_get_settings()["stale_summary_message_threshold"])
        )
        threads = self.get_all_threads()
        if not threads:
            return []

        thread_ids = [t["id"] for t in threads]
        placeholders = ", ".join("?" * len(thread_ids))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT thread_id, content FROM messages "
                f"WHERE thread_id IN ({placeholders}) AND role = 'user'",
                thread_ids,
            ).fetchall()

        # Count non-junk messages per thread in Python
        counts: dict[str, int] = {}
        for row in rows:
            if not _is_junk_message(row["content"]):
                counts[row["thread_id"]] = counts.get(row["thread_id"], 0) + 1

        stale = []
        for t in threads:
            tid = t["id"]
            msg_count = counts.get(tid, 0)
            summarized: int = int(t.get("summarized_msg_count") or 0)
            delta: int = msg_count - summarized
            if delta >= limit:
                stale.append({**t, "delta": delta, "msg_count": msg_count})
        return stale

    def get_archive_candidates(self) -> list[dict]:
        """Return threads that are candidates for archiving.

        A thread qualifies if ANY of:
          - status == 'done'
          - status == 'failed'
          - last_active > 48 hours ago AND status NOT IN ('background', 'waiting')
          - status == 'idle' AND last_active > 24 hours ago

        Excludes the current thread and already-archived threads.
        """
        current_thread = self.get_current_thread()
        threads = self.get_all_threads()
        candidates = []
        for t in threads:
            tid = t["id"]
            status = t.get("status") or "active"

            if tid == current_thread or status == "archived":
                continue

            if status in ("done", "failed", "closed"):
                candidates.append(t)
                continue

            age = _thread_age_seconds(t.get("last_active") or "")
            if (
                age is not None
                and age > _get_settings()["thread_archive_threshold_secs"]
                and status not in ("background", "waiting")
            ):
                candidates.append(t)

        return candidates
