"""dbops.threads — Thread CRUD, state machine, archive, and stale-query mixin.

Owns: create/get/update/list threads, thread status transitions, archive/
unarchive, stale-thread detection, and archive-candidate selection.
Must not own: message content, project assignment, agent pool, notifications.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import NoReturn

import dbops.schema as _schema
from dbops.schema import (
    _get_settings,
    _is_junk_message,
    _thread_age_seconds,
)
from dbops.conv_node_mirror import mirror_conv_insert, mirror_conv_update
from dbops.slug_alloc import LIVE_NODE_STATES, LIVE_SLUG_STATES, next_wheel_slug
from dbops.state_write import write_state

# Read MAX_THREADS via module reference so tests can patch dbops.threads.MAX_THREADS
# (or dbops.schema.MAX_THREADS) to bypass the cap in seeding fixtures.
MAX_THREADS = _schema.MAX_THREADS

# Bounded retries for the atomic BEGIN IMMEDIATE allocation loop (lock-contention
# backstop; the write lock itself prevents duplicate-slug races).
_ALLOC_ATTEMPTS = 5

# Lexical thread-title dedup scorer lives in dbops.thread_dedup (extracted for the
# loc-gate budget). Re-exported here so existing importers (juggle_cli_common,
# tests) keep `from dbops.threads import _title_similarity, THREAD_DEDUP_THRESHOLD`.
from dbops.thread_dedup import (  # noqa: E402,F401
    THREAD_DEDUP_THRESHOLD,
    _normalize_title_tokens,
    _title_similarity,
)

# Statuses considered OPEN (live work). Closed/archived threads are historical
# and are NEVER reuse targets. The SINGLE source of truth lives in
# dbops.slug_alloc (must match the partial unique index idx_threads_live_label).
_OPEN_THREAD_STATES = LIVE_SLUG_STATES

# Node-vocab equivalents of LIVE_SLUG_STATES for the conversation READ-collapse
# (P8 Task 4.2): the get_* readers resolve from kind='conversation' nodes, whose
# `state` column uses node vocab. 'open' ≡ legacy 'active' (bijective map in
# dbops.node_translation). The legacy `threads` WRITE path (create/unarchive/
# slug_alloc) still uses status vocab — it is cut in the later write-cut node.
#
# SINGLE source of truth: dbops.slug_alloc.LIVE_NODE_STATES, which is kept in
# lock-step with the partial unique index idx_nodes_live_label (Migration 54).
# Aliased (not re-literaled) so the cap count, the read-collapse scans, and the
# slug-allocation live-set can never diverge on what "live" means.
_LIVE_NODE_STATES = LIVE_NODE_STATES


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

        Safety: only OPEN threads are eligible, a thread that already OWNS a
        graph topic or task is excluded — those are real in-flight work and must
        never be collapsed into another topic — and (2026-07-09, D2 incident) a
        thread with a currently BUSY agent assigned is excluded too: an idle
        ad-hoc conversation is a legitimate reuse target (the 2026-06-15 intent
        — collapsing "[A] slug wheel" and the tick's own "[T-slug-wheel] Topic
        Slug Wheel..." dispatch into one thread), but a thread an agent is
        actively mid-flight on is not, no matter how strong the lexical match —
        reusing it would rebind + re-dispatch into a live agent's conversation
        (run misattribution, same class of hazard the F2 fan-in guard defends
        against for topic-to-topic reuse in reusable_thread()).
        """
        _ph = ",".join("?" * len(_LIVE_NODE_STATES))
        with self._connect() as conn:
            if project_id is not None:
                rows = conn.execute(
                    "SELECT id, title FROM nodes WHERE kind='conversation' "
                    f"AND state IN ({_ph}) AND project_id = ?",
                    (*_LIVE_NODE_STATES, project_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, title FROM nodes WHERE kind='conversation' "
                    f"AND state IN ({_ph})",
                    _LIVE_NODE_STATES,
                ).fetchall()
            # Threads that already OWN a graph topic/task are bound via the typed
            # kind='dispatch' node_edge (depends_on_id = the conversation node).
            # P8 c4-write-cut: read that edge, not the retired graph_*.thread_id.
            try:
                owned: set[str] = {
                    r["depends_on_id"]
                    for r in conn.execute(
                        "SELECT depends_on_id FROM node_edges WHERE kind='dispatch'"
                    ).fetchall()
                }
            except sqlite3.OperationalError:
                owned = set()  # node_edges absent on a pre-migration DB
            busy: set[str] = {
                r["assigned_thread"]
                for r in conn.execute(
                    "SELECT assigned_thread FROM agents "
                    "WHERE status='busy' AND assigned_thread IS NOT NULL"
                ).fetchall()
            }
        for row in rows:
            if row["id"] in owned or row["id"] in busy:
                continue
            candidate = row["title"] or ""
            if _title_similarity(topic, candidate) >= THREAD_DEDUP_THRESHOLD:
                return row["id"]
        return None

    def _insert_new_conversation(
        self, conn, new_id: str, topic: str, session_id: str, now_min: str
    ) -> str | None:
        """Cap-check + slug-alloc + conversation-node insert on the caller's
        ``conn`` (caller MUST already hold the write lock). Returns ``new_id``, or
        ``None`` if at/over the thread cap.

        The SOLE new-conversation writer — shared by BOTH the self-transaction
        ``create_thread`` path and the caller-transaction (``conn=``) path so a
        thread is allocated + laid down identically whether created standalone or
        inside a larger atomic write (one source of truth). P8 c4-write-cut: the
        cap count and live-set scan both resolve from kind='conversation' nodes,
        whose unique idx_nodes_live_label enforces the no-shared-live-slug rule."""
        rows = conn.execute(
            "SELECT state FROM nodes WHERE kind='conversation'"
        ).fetchall()
        # Count only genuinely-LIVE conversations against the cap (2026-07-07
        # incident: counting `state != 'archived'` let terminal 'done'/'failed-exec'
        # rows occupy cap slots, so the cap became unescapable once >= MAX_THREADS
        # terminal conversations accumulated — archiving 17 freed nothing). The live
        # predicate is idx_nodes_live_label's (open/running/background), so the count
        # and the no-shared-live-slug uniqueness invariant stay in lock-step.
        active_count = sum(1 for r in rows if r["state"] in _LIVE_NODE_STATES)
        if active_count >= MAX_THREADS:
            return None
        user_label = self._next_wheel_slug(conn)
        # The conversation is a first-class node — the sole store.
        mirror_conv_insert(
            conn, new_id, topic=topic, session_id=session_id,
            user_label=user_label, now=now_min,
        )
        return new_id

    def create_thread(
        self, topic: str, session_id: str, project_id: str | None = None,
        *, conn=None
    ) -> str:
        """Create a new thread. Returns the UUID of the new thread.

        Assigns next available A–Z label. Raises ValueError if 10 non-archived
        threads already exist or all 26 labels are in use.

        Dedup guard: if an OPEN (same-project, when `project_id` is given)
        thread already exists whose title is a lexical duplicate of `topic`,
        no new row is inserted and that existing thread's id is returned.
        2026-07-09 (D2): a thread with a currently BUSY agent assigned is
        never a reuse target, even on a lexical match — see
        ``_find_duplicate_open_thread``.

        When ``conn`` is passed the conversation node is written on the CALLER'S
        connection/transaction (no BEGIN/COMMIT of our own), so thread creation can
        be one step of a larger atomic write — e.g. ``create_loop_atomic`` binds a
        loop to its own thread inside the same all-or-nothing transaction that lays
        down the project + graph (the caller already holds the write lock, and its
        rollback discards the thread too). Over-cap still fails loud via
        ``_raise_thread_cap`` — which rolls the caller's transaction back.
        """
        existing = self._find_duplicate_open_thread(topic, project_id)
        if existing is not None:
            return existing
        new_id = str(uuid.uuid4())
        now_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        if conn is not None:
            result = self._insert_new_conversation(
                conn, new_id, topic, session_id, now_min
            )
            if result is None:
                self._raise_thread_cap()  # rolls the caller's transaction back
            return result
        # ATOMIC allocation (2026-06-21): take the write lock with BEGIN IMMEDIATE
        # BEFORE reading label_seq so the read-modify-write of the counter and the
        # live-set scan are serialized across processes — no two creates can land
        # on the same slug. The retry loop is a backstop for lock contention.
        with self._connect() as conn:
            conn.isolation_level = None  # manual transaction control
            last_exc: Exception | None = None
            for _attempt in range(_ALLOC_ATTEMPTS):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    last_exc = exc  # busy; retry
                    continue
                try:
                    result = self._insert_new_conversation(
                        conn, new_id, topic, session_id, now_min
                    )
                    if result is None:
                        conn.execute("ROLLBACK")
                        break  # over cap — raise structured guidance below
                    conn.execute("COMMIT")
                    return result
                except sqlite3.IntegrityError as exc:
                    conn.execute("ROLLBACK")
                    last_exc = exc
                    if "user_label" not in str(exc) and "idx_nodes_live_label" not in str(exc):
                        raise
                    continue  # backstop; BEGIN IMMEDIATE should prevent this
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            else:
                raise RuntimeError(
                    f"create_thread: could not allocate a slug after "
                    f"{_ALLOC_ATTEMPTS} attempts"
                ) from last_exc
        self._raise_thread_cap()  # reached only via the over-cap break

    def _raise_thread_cap(self) -> NoReturn:
        """Raise a ValueError when MAX_THREADS live threads already exist,
        surfacing the archivable candidates as actionable guidance."""
        candidates = self.get_archive_candidates()
        if candidates:
            cmds = ", ".join(
                f"[{t.get('user_label') or t.get('label')}] "
                f"{(t.get('title') or t.get('topic') or '')[:40]}"
                f" → archive-thread {t.get('user_label') or t.get('label')}"
                for t in candidates[:5]
            )
            raise ValueError(
                f"Maximum of {MAX_THREADS} threads already exist. Archivable: {cmds}"
            )
        raise ValueError(
            f"Maximum of {MAX_THREADS} threads already exist. "
            "No immediate candidates — close or archive a thread manually."
        )

    def _next_wheel_slug(self, conn) -> str:
        """Thin seam over slug_alloc.next_wheel_slug (caller holds write lock)."""
        return next_wheel_slug(conn)

    def get_thread(self, thread_id: str) -> dict | None:
        """Look up a conversation by its UUID `id`. Returns None if not found.

        P8 Task 4.2 (conversation read-collapse): reads the authoritative
        kind='conversation' node. The returned dict carries NODE vocab —
        ``state``/``title``/``last_active_at`` — and the legacy ``status``/
        ``topic``/``last_active`` keys are GONE (Q1, no shim). Callers adopt the
        node vocab directly. The legacy `threads` WRITE path still mirrors here,
        so a migrated DB always has the node; it is cut in the write-cut node."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE id = ? AND kind='conversation'",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def get_thread_by_user_label(self, label: str | None) -> dict | None:
        """Resolve a user-typed slug to a conversation — the SINGLE chokepoint.

        Newest-wins (T-slug-wheel): since slugs rotate and persist on closed/
        archived rows, a reused slug always resolves to the NEWEST holder —
        a live (open/running/background) holder first, then the most recently
        created terminal holder. Case-insensitive. Returns None if not found.

        P8 Task 4.2: reads kind='conversation' nodes (node vocab). Every feature
        that maps a user-typed slug -> conversation MUST route through this
        function so reuse resolves consistently everywhere.
        """
        if not label:
            return None
        _ph = ",".join("?" * len(_LIVE_NODE_STATES))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM nodes WHERE kind='conversation' "
                "AND user_label = ? COLLATE NOCASE "
                f"ORDER BY (CASE WHEN state IN ({_ph}) THEN 0 ELSE 1 END), "
                # created_at on a conversation node is minute-precision (the mirror
                # writes now_min), so rowid DESC is the stable newest-wins tiebreak
                # for two holders created within the same minute (T-slug-wheel).
                "created_at DESC, rowid DESC "
                "LIMIT 1",
                (label, *_LIVE_NODE_STATES),
            ).fetchone()
        return dict(row) if row else None

    def get_all_threads(self) -> list[dict]:
        """All conversations as node-vocab dicts (P8 Task 4.2: from `nodes`)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE kind='conversation' ORDER BY created_at"
            ).fetchall()
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
        # P8 c4-write-cut: nodes is the sole conversation store — mirror_conv_update
        # maps status→state, renames topic/last_active, and drops columns the node
        # lacks (reviewed/assigned_confidence, already non-functional post read-flip).
        with self._connect() as conn:
            mirror_conv_update(conn, thread_id, **kwargs)
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
            # P8 c4-write-cut: nodes is the sole conversation store. mirror_conv_update
            # maps the legacy status→node state (active→open, closed→done, archived→
            # archived, background→background) so nodes.state is the truth — the fix
            # for the archive/close-state divergence defect. T-slug-wheel: the slug
            # stays on the node through any terminal transition (historical handle).
            mirror_conv_update(conn, thread_id, status=status, last_active_at=now)
            conn.commit()

    def set_conversation_background(self, thread_id: str) -> None:
        """Mark a conversation node background (a dispatched agent owns it).

        P8 c3-write-cut: ``nodes`` is the SOLE conversation writer for the
        ``'background'`` state — the legacy ``threads.status='background'`` write
        is gone. Writes ``nodes.state='background'`` in one transaction via the
        unified state-writer (its graph_tasks/graph_topics mirror no-ops for a
        conversation id). Background-ness is now READ from ``nodes.state`` (the
        watchdog reaper + cockpit panels flipped in c3-reads).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            write_state(conn, thread_id, "background", now=now)
            conn.commit()

    def touch_last_active(self, thread_id: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            # P8 c4-write-cut: nodes is the sole conversation store.
            mirror_conv_update(conn, thread_id, last_active_at=now)
            conn.commit()

    def get_threads_by_status(self, state: str) -> list[dict]:
        """Conversations in a given node ``state`` (P8 Task 4.2: from `nodes`).

        ``state`` is a NODE-vocab value (e.g. 'open', 'running', 'done',
        'archived') — callers pass node states, not legacy statuses."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM nodes WHERE kind='conversation' AND state = ? "
                "ORDER BY last_active_at DESC",
                (state,),
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
            # P8 c4-write-cut: nodes is the sole conversation store (status='archived'
            # maps to state='archived').
            mirror_conv_update(
                conn, thread_id, status="archived", show_in_list=0, last_active_at=now
            )
            conn.commit()

    def unarchive_thread(self, thread_id: str) -> str:
        """Unarchive: status=active, show_in_list=1.

        T-slug-wheel: the archived row kept its slug, so reuse it when no other
        NON-ARCHIVED row currently holds it (2026-07-08: widened past the live
        subset — open/running/background — because a 'done'-but-unarchived row
        can have taken over the slug via the wheel while this one sat archived);
        otherwise allocate a fresh slug off the wheel. BEGIN IMMEDIATE makes the
        read-decide-write atomic across processes (2026-07-08: this read-then-
        write was previously a plain deferred transaction, a TOCTOU gap next to
        create_thread's atomic allocation)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            conn.isolation_level = None  # manual transaction control
            last_exc: Exception | None = None
            for _attempt in range(_ALLOC_ATTEMPTS):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    last_exc = exc  # busy; retry
                    continue
                try:
                    new_label = self._unarchive_pick_label(conn, thread_id)
                    # status='active' maps to state='open'; the unique
                    # idx_nodes_live_label holds because new_label is free
                    # among non-archived nodes.
                    mirror_conv_update(
                        conn, thread_id, status="active", show_in_list=1,
                        user_label=new_label, last_active_at=now,
                    )
                    conn.execute("COMMIT")
                    return new_label
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            raise RuntimeError(
                f"unarchive_thread: could not allocate a slug after "
                f"{_ALLOC_ATTEMPTS} attempts"
            ) from last_exc

    def _unarchive_pick_label(self, conn, thread_id: str) -> str:
        """Caller holds the write lock. Reuse the row's own slug unless some
        OTHER non-archived row now holds it; else allocate a fresh one."""
        cur = conn.execute(
            "SELECT user_label FROM nodes WHERE id = ? AND kind='conversation'",
            (thread_id,),
        ).fetchone()
        existing = cur["user_label"] if cur else None
        held = {
            row["user_label"]
            for row in conn.execute(
                "SELECT user_label FROM nodes WHERE kind='conversation' "
                "AND user_label IS NOT NULL AND state != 'archived' AND id != ?",
                (thread_id,),
            ).fetchall()
        }
        return existing if existing and existing not in held else self._next_wheel_slug(conn)

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
        """Return conversations that are candidates for archiving (node vocab).

        A conversation qualifies if ANY of:
          - state in ('done', 'failed-exec')  (terminal: 'done' covers legacy
            closed+done via the bijective status↔state map)
          - last_active_at > 48 hours ago AND state != 'background'

        Excludes the current conversation and already-archived ones.
        """
        current_thread = self.get_current_thread()
        threads = self.get_all_threads()
        candidates = []
        for t in threads:
            tid = t["id"]
            state = t.get("state") or "open"

            if tid == current_thread or state == "archived":
                continue

            if state in ("done", "failed-exec"):
                candidates.append(t)
                continue

            age = _thread_age_seconds(t.get("last_active_at") or "")
            if (
                age is not None
                and age > _get_settings()["thread_archive_threshold_secs"]
                and state != "background"
            ):
                candidates.append(t)

        return candidates
