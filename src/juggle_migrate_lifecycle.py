"""One-shot migration: legacy thread statuses → new 4-state machine.

Safe to run multiple times (idempotent). Run via:
    python -m juggle_migrate_lifecycle
"""
import json
from datetime import datetime, timezone

from juggle_db import JuggleDB, _next_excel_label


def migrate(db: JuggleDB) -> dict:
    """Migrate legacy thread statuses. Returns a summary dict."""
    stats = {"done": 0, "background": 0, "failed": 0, "needs_action": 0,
             "backfill_label": 0, "backfill_last_active": 0}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    # Fetch all rows under raw SQL (avoid set_thread_status which rejects legacy values)
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM threads ORDER BY created_at"
        ).fetchall()]

    # --- Status remap ---
    for t in rows:
        tid = t["id"]
        status = t.get("status")
        open_questions_raw = t.get("open_questions") or "[]"
        try:
            open_questions = json.loads(open_questions_raw) if isinstance(open_questions_raw, str) else (open_questions_raw or [])
        except (json.JSONDecodeError, ValueError):
            open_questions = []

        if status == "done":
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET status = 'closed', last_active_at = COALESCE(last_active_at, ?) "
                    "WHERE id = ?",
                    (now, tid),
                )
                conn.commit()
            stats["done"] += 1

        elif status == "background":
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET status = 'running', last_active_at = COALESCE(last_active_at, ?) "
                    "WHERE id = ?",
                    (now, tid),
                )
                conn.commit()
            stats["background"] += 1

        elif status == "failed":
            if open_questions:
                for q in open_questions:
                    text = q.get("text") if isinstance(q, dict) else str(q)
                    db.add_action_item(thread_id=tid, message=text,
                                       type_="failure", priority="high")
            else:
                agent_result = t.get("agent_result") or "failed"
                with db._connect() as conn:
                    srow = conn.execute("SELECT value FROM session WHERE key = 'session_id'").fetchone()
                session_id = srow["value"] if srow else ""
                db.add_notification_v2(
                    thread_id=tid,
                    message=f"failed: {agent_result}",
                    session_id=session_id,
                )
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET status = 'closed', open_questions = '[]', "
                    "last_active_at = COALESCE(last_active_at, ?) WHERE id = ?",
                    (now, tid),
                )
                conn.commit()
            stats["failed"] += 1

        elif status == "needs_action":
            for q in open_questions:
                text = q.get("text") if isinstance(q, dict) else str(q)
                db.add_action_item(thread_id=tid, message=text,
                                   type_="question", priority="normal")
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET status = 'closed', open_questions = '[]', "
                    "last_active_at = COALESCE(last_active_at, ?) WHERE id = ?",
                    (now, tid),
                )
                conn.commit()
            stats["needs_action"] += 1

    # --- Backfill user_label for any row lacking one (in creation order) ---
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, user_label, created_at FROM threads ORDER BY created_at"
        ).fetchall()]
        used = {r["user_label"] for r in rows if r["user_label"]}
        for r in rows:
            if not r["user_label"]:
                lbl = _next_excel_label(used)
                conn.execute(
                    "UPDATE threads SET user_label = ? WHERE id = ?",
                    (lbl, r["id"]),
                )
                used.add(lbl)
                stats["backfill_label"] += 1
        conn.commit()

    # --- Backfill last_active_at from last_active (or created_at) for any null ---
    with db._connect() as conn:
        null_rows = conn.execute(
            "SELECT id, last_active, created_at FROM threads WHERE last_active_at IS NULL"
        ).fetchall()
        for r in null_rows:
            src = r["last_active"] or r["created_at"] or now
            # Normalise to minute precision
            s = src.replace("T", " ").replace("Z", "")[:16]
            conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE id = ?",
                (s, r["id"]),
            )
            stats["backfill_last_active"] += 1
        conn.commit()

    return stats


if __name__ == "__main__":
    db = JuggleDB()
    db.init_db()
    result = migrate(db)
    print("Migration complete:")
    for k, v in result.items():
        print(f"  {k}: {v}")
