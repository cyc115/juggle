"""juggle_spool_dead — dead-letter management (RCA D1, 2026-07-04).

Owns everything about the ``spool/dead/`` subdir: the drain's per-event
dead-letter action items (moved here from juggle_spool_apply so that module
stays under the LOC gate), a low-frequency backlog reminder so a silently
rotting dead-letter pile cannot persist unseen, and the recovery primitives
(list / replay / purge) behind the ``juggle spool`` CLI.

Reuses dbops.spool + juggle_spool_paths; NEVER touches the write/agent-context
path (juggle_spool_cli_common) — recovery is a read+requeue surface only.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from juggle_spool_paths import spool_dead_dir, spool_dir

# ── transient-missing retry classifier (T-fix-spool-drain-systemexit) ──────────
# A not-found precondition (target thread/task absent) is the handler's FIRST
# check — side-effect-free and often TRANSIENT: the target row is created moments
# earlier by another context and lands on a LATER drain (every dead-lettered
# event applied cleanly on manual replay). drain_spool retries it in-order for a
# bounded number of ticks instead of dead-lettering the first one; a genuinely
# absent target still dead-letters once attempts exhaust. Keys on the handler's
# captured "not found"/"no thread" text — a contradiction (illegal transition,
# missing arg) lacks those markers and dead-letters at once.
_RETRY_MAX_ATTEMPTS = 3
_TRANSIENT_MISSING_MARKERS = ("not found", "no thread")


def _is_transient_missing(msg: str) -> bool:
    m = (msg or "").lower()
    return any(marker in m for marker in _TRANSIENT_MISSING_MARKERS)


# ── per-drain dead-letter action items (moved verbatim from juggle_spool_apply) ──
# A burst of empty/malformed junk events (e.g. the 2026-07-02 empty-args
# agent_complete fixtures) must NOT flood the cockpit with one HIGH action item
# each — cap per-event items and collapse the overflow into a single grouped
# alert so the signal survives without the noise.
_DEAD_ACTION_ITEM_CAP = 3


def file_dead_letter_action_items(db, dead: list[tuple[str, str, str, str]]) -> None:
    """dead: (thread_id, uuid, type, msg) tuples freshly dead-lettered this drain."""
    if not dead:
        return
    if len(dead) <= _DEAD_ACTION_ITEM_CAP:
        for thread_id, uuid, etype, msg in dead:
            db.add_action_item(
                thread_id=thread_id or None,
                message=f"⚠️ Spool event {uuid} ({etype}) dead-lettered: {msg}",
                type_="failure",
                priority="high",
            )
        return
    sample = ", ".join(f"{etype}:{uuid[:8]}" for _, uuid, etype, _ in dead[:_DEAD_ACTION_ITEM_CAP])
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ {len(dead)} spool events dead-lettered this drain "
            f"(first {_DEAD_ACTION_ITEM_CAP}: {sample}). See spool/dead/ for full reasons."
        ),
        type_="failure",
        priority="high",
    )


# ── low-frequency backlog reminder (RCA D1 req 3) ──────────────────────────────
_DEAD_REMINDER_INTERVAL_S = 24 * 3600
_REMINDER_MARKER = ".dead_reminder_at"


def maybe_file_dead_backlog_reminder(db, new_dead: int = 0) -> bool:
    """File ONE low-frequency (>=24h apart) action item while dead/ is non-empty,
    so a silently rotting dead-letter backlog can't persist unseen. No-op when
    this drain already filed fresh per-event items (new_dead>0), when dead/ is
    empty, or when a reminder was filed within the interval. A per-spool marker
    file throttles it — this is called every tick but must NOT re-file every
    tick. Returns True iff a reminder was filed."""
    if new_dead:
        return False
    dead = spool_dead_dir()
    count = len(list(dead.glob("*.json"))) if dead.exists() else 0
    if count == 0:
        return False
    marker = spool_dir() / _REMINDER_MARKER
    now = datetime.now(timezone.utc)
    if marker.exists():
        try:
            last = datetime.fromisoformat(marker.read_text().strip())
            if (now - last).total_seconds() < _DEAD_REMINDER_INTERVAL_S:
                return False
        except (ValueError, OSError):
            pass
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ {count} dead-lettered spool event(s) awaiting recovery — "
            "triage with `juggle spool list-dead`, then replay or purge."
        ),
        type_="failure",
        priority="normal",
    )
    try:
        marker.write_text(now.isoformat())
    except OSError:
        pass
    return True


# ── recovery primitives (juggle spool list-dead / replay / purge) ──────────────
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def list_dead_entries() -> list[dict]:
    """One dict per dead file (oldest-first): uuid/type/created_at/dead_reason/file."""
    dead = spool_dead_dir()
    if not dead.exists():
        return []
    out: list[dict] = []
    for p in sorted(dead.glob("*.json")):
        payload = _load(p)
        out.append({
            "uuid": payload.get("uuid", p.stem),
            "type": payload.get("type", "unknown"),
            "created_at": payload.get("created_at", ""),
            "dead_reason": payload.get("dead_reason", ""),
            "file": p.name,
        })
    return out


def _journal_outcome(db, uuid: str) -> str | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT outcome FROM spool_journal WHERE uuid = ?", (uuid,)
        ).fetchone()
    return row["outcome"] if row else None


def _clear_journal(db, uuid: str) -> None:
    with db._connect() as conn:
        conn.execute("DELETE FROM spool_journal WHERE uuid = ?", (uuid,))
        conn.commit()


def replay_dead_file(db, dead_path: Path, force_applying: bool = False) -> tuple[bool, str]:
    """Re-inject one dead file into the pending spool dir so the next watchdog
    drain retries it. Clears any stale spool_journal row first (a 'failed' /
    'applying' outcome would otherwise short-circuit apply_event). Refuses an
    'applying' row unless force_applying — a prior apply crashed mid-flight and
    its handler's non-transactional side effects (git integrate/push) may be
    partially applied. Returns (ok, uuid-or-error-message)."""
    payload = _load(dead_path)
    if not payload:
        return False, f"{dead_path.name}: unreadable dead file"
    uuid = payload.get("uuid", dead_path.stem)
    if _journal_outcome(db, uuid) == "applying" and not force_applying:
        return False, (
            f"{uuid}: spool_journal still 'applying' (a prior apply was interrupted "
            "mid-flight; retry may double-apply side effects). Pass --force-applying "
            "to override."
        )
    _clear_journal(db, uuid)
    payload.pop("dead_reason", None)
    dest = spool_dir() / dead_path.name
    tmp = spool_dir() / f".{dest.name}.tmp"
    tmp.write_text(json.dumps(payload))
    tmp.replace(dest)  # atomic on the same filesystem
    dead_path.unlink(missing_ok=True)
    return True, uuid


def replay_dead(db, uuid: str | None = None, all_: bool = False,
                force_applying: bool = False) -> list[tuple[bool, str]]:
    """Replay dead files: every file when all_, else those whose uuid matches
    (exact or unique-prefix). Returns one (ok, msg) per targeted file."""
    dead = spool_dead_dir()
    files = sorted(dead.glob("*.json")) if dead.exists() else []
    if all_:
        targets = files
    else:
        targets = [p for p in files
                   if uuid and (lambda u: u == uuid or u.startswith(uuid))(_load(p).get("uuid", p.stem))]
    return [replay_dead_file(db, p, force_applying) for p in targets]


def purge_dead(before: str | None = None) -> int:
    """Delete dead files (all, or only those created strictly before the given
    YYYY-MM-DD). Files with no/undated created_at are KEPT when --before is set
    (can't confirm they're old). Returns the number deleted."""
    dead = spool_dead_dir()
    files = sorted(dead.glob("*.json")) if dead.exists() else []
    removed = 0
    for p in files:
        if before:
            created = _load(p).get("created_at", "")
            if not created or created[:10] >= before:
                continue
        p.unlink(missing_ok=True)
        removed += 1
    return removed
