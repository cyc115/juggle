"""dbops.selfheal_audit — Self-heal v2 P2 grouping/audit/lease mixin for JuggleDB.

Owns the P2 additions kept OUT of dbops/selfheal.py (≤300-line architecture gate):
the grouped (group_key) view with breadth re-split, the durable selfheal_audit
log, batched per-signature resolution, the set-once benign lease, and the
audited+gated silent auto-hide.

Must not own: error capture / dedup (dbops/selfheal.py) or pure triage logic
(selfheal_triage.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

_log = logging.getLogger(__name__)


def _nowstr() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


class SelfhealAuditMixin:
    """Mixin: grouped view, durable audit log, batched resolve, lease, silent-hide."""

    # ---- grouped view (breadth re-split) ---------------------------------
    def get_grouped_error_events(
        self, status: str | None = None, include_hidden: bool = False,
        broad_cap: int = 10,
    ) -> list[dict]:
        """Aggregate the triage row set by ``group_key`` (selfheal v2 P2).

        Each group: {group_key, total_count, distinct_signatures, representative
        (newest row), broad (distinct > broad_cap), members (re-split payload:
        the per-signature rows when broad, else None), statuses}.

        DA fix b: a BROAD group is REALLY re-split — its member signatures are
        surfaced (``members`` populated) so an over-aggregated group can never
        silently swallow a new bug behind one collapsed row. The exact
        signature_hash leaf is never destroyed.
        """
        rows = self.get_open_error_events(status=status, include_hidden=include_hidden)
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r.get("group_key") or "", []).append(r)
        out: list[dict] = []
        for gk, grp in groups.items():
            by_sig: dict[str, dict] = {}
            for r in grp:
                s = r.get("signature_hash") or ""
                prev = by_sig.get(s)
                if prev is None or (r.get("last_seen") or "") >= (prev.get("last_seen") or ""):
                    by_sig[s] = r
            members = sorted(by_sig.values(), key=lambda r: r.get("last_seen") or "", reverse=True)
            distinct = len(by_sig)
            broad = distinct > broad_cap
            out.append({
                "group_key": gk,
                "total_count": sum(r.get("count") or 0 for r in grp),
                "distinct_signatures": distinct,
                "representative": members[0] if members else {},
                "broad": broad,
                "members": members if broad else None,
                "statuses": sorted({r.get("status") for r in grp if r.get("status")}),
            })
        # Broad (over-aggregation risk) first, then by volume, then key — stable.
        out.sort(key=lambda g: (not g["broad"], -g["total_count"], g["group_key"]))
        return out

    # ---- durable audit log ------------------------------------------------
    def _audit_insert(self, conn, event_id, signature_hash, group_key, action,
                      reason=None, detail=None, *, _guard: bool = False) -> None:
        """Insert ONE selfheal_audit row on the given connection (no commit).

        Single source of truth for the audit INSERT — shared by record_selfheal_audit
        (own transaction) and the in-transaction silent-hide / new-variant paths.
        With ``_guard=True`` a missing table is swallowed (capture-path callers
        must never break on a pre-migration DB); otherwise it raises (fail loud).
        """
        try:
            conn.execute(
                "INSERT INTO selfheal_audit "
                "(ts, event_id, signature_hash, group_key, action, reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_nowstr(), event_id, signature_hash, group_key, action, reason, detail),
            )
        except Exception:
            if _guard:
                return
            raise

    def record_selfheal_audit(self, event_id, signature_hash, group_key, action,
                              reason=None, detail=None) -> None:
        """Durably record one self-heal hide/resurface/lease event (own transaction)."""
        with self._connect() as conn:
            self._audit_insert(conn, event_id, signature_hash, group_key, action, reason, detail)
            conn.commit()

    def get_selfheal_audit(self, limit: int = 50, action: str | None = None) -> list[dict]:
        """Return recent audit rows (newest first), optionally filtered by action."""
        with self._connect() as conn:
            if action is not None:
                rows = conn.execute(
                    "SELECT * FROM selfheal_audit WHERE action = ? ORDER BY id DESC LIMIT ?",
                    (action, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM selfheal_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def has_selfheal_audit_table(self) -> bool:
        """True if the durable audit table exists — HARD precondition for silent-hide."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='selfheal_audit'"
            ).fetchone() is not None

    # ---- batched per-signature resolution --------------------------------
    def resolve_signatures(self, signature_hashes: list[str],
                           action_item_id: int | None = None) -> int:
        """Resolve EXACTLY the given signatures (returns count updated).

        Never resolves siblings sharing the group_key that were not passed — a
        fix touches specific variants; un-fixed variants stay open so a
        regression re-alerts (research §5.6). Guards the empty list (DA fix c):
        no signatures → 0, never a malformed ``IN ()``.
        """
        if not signature_hashes:
            return 0
        placeholders = ",".join("?" for _ in signature_hashes)
        now = _nowstr()
        with self._connect() as conn:
            if action_item_id is not None:
                cur = conn.execute(
                    f"UPDATE error_events SET status='resolved', action_item_id=?, "
                    f"last_seen=? WHERE signature_hash IN ({placeholders})",
                    (action_item_id, now, *signature_hashes),
                )
            else:
                cur = conn.execute(
                    f"UPDATE error_events SET status='resolved', last_seen=? "
                    f"WHERE signature_hash IN ({placeholders})",
                    (now, *signature_hashes),
                )
            conn.commit()
            return cur.rowcount

    # ---- set-once benign lease -------------------------------------------
    def set_benign_lease(self, event_id: int, lease_days: int) -> bool:
        """Set ``benign_until = now + lease_days`` (set-once anchor, independent
        of last_seen). Returns True if a row was updated."""
        until = (datetime.now(timezone.utc) + timedelta(days=lease_days)).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE error_events SET benign_until=? WHERE id=?", (until, event_id))
            conn.commit()
            return cur.rowcount == 1

    # ---- audited + gated silent auto-hide (Task 5) -----------------------
    def silent_autohide(self, event_id: int, *, reason: str, lease_days: int,
                        detail: str | None = None) -> bool:
        """Atomically silent-hide a benign verdict: write the audit row FIRST,
        then flip status→non_issue and set the lease — ALL in ONE transaction.

        DA fix 🔴: a FAILED audit write must NEVER produce a silent terminal hide.
        Because the audit INSERT runs (and must succeed) before the status flip in
        the same uncommitted transaction, any failure rolls the whole thing back
        and the row stays non-terminal. Returns True on hide, False if the row is
        gone. Raises (and leaves status unchanged) if the audit write fails.
        """
        until = (datetime.now(timezone.utc) + timedelta(days=lease_days)).strftime("%Y-%m-%d %H:%M")
        now = _nowstr()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT signature_hash, group_key FROM error_events WHERE id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                return False
            try:
                # AUDIT FIRST — a failure here aborts BEFORE any status flip.
                self._audit_insert(
                    conn, event_id, row["signature_hash"], row["group_key"],
                    "silent_autohide", reason, detail)
                conn.execute(
                    "UPDATE error_events SET status='non_issue', benign_until=?, "
                    "last_seen=? WHERE id=?",
                    (until, now, event_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True
