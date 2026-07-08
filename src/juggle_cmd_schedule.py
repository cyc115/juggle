"""juggle_cmd_schedule — the `schedule list` / `schedule delete` verbs (loop V2, P0b).

ONE schedule namespace. `schedule create` stays a router PROMPT
(commands/schedule:create.md); the deterministic `list`/`delete` behaviour lives
HERE in code (code over prompts) so it is unit-testable in Python.

Two kinds of scheduled thing share the namespace, each TYPE-TAGGED:
  - OS SCHEDULE — a script/binary on a launchd/systemd/cron timer
    (``juggle_scheduler`` backend: ``list_tasks`` / ``uninstall``).
  - LOOP — a recurring agent-work topic Juggle drives (``dbops.loops``:
    ``list_active_loops`` / ``pause_loop`` / ``delete_loop`` + project-close).

``delete`` is SOFT by default and type-dispatched: a loop is paused and its
project closed (recoverable via ``/juggle:project:open`` + resume); an OS
schedule is uninstalled. ``--purge`` hard-removes a loop row (non-recoverable).
An unknown id fails loud — never a silent no-op.
"""
from __future__ import annotations

from juggle_cli_common import get_db


def _get_backend(backend):
    """Resolve the OS scheduler backend, or ``None`` if this host has none
    available (Linux without systemd/cron, etc.). An injected ``backend`` (tests)
    is returned as-is."""
    if backend is not None:
        return backend
    try:
        from juggle_scheduler import get_backend
        return get_backend()
    except Exception:
        return None


def _os_tasks(backend, *, strict: bool) -> list:
    """List the OS scheduled tasks off ``backend`` (``[]`` if no backend).

    ``strict`` deliberately splits the two call sites:
      - LIST / display (``strict=False``): an enumeration hiccup yields ``[]`` —
        parity with ``juggle_cockpit_sched.fetch_scheduled_tasks``; a read must
        never blow up.
      - DELETE / dispatch (``strict=True``): the failure PROPAGATES. Swallowing
        it here would misclassify a real, still-installed schedule as an unknown
        id and report a no-op "unknown schedule" instead of the true backend
        error (detect-refuse-preserve, not a silent lie)."""
    if backend is None:
        return []
    if strict:
        return list(backend.list_tasks())
    try:
        return list(backend.list_tasks())
    except Exception:
        return []


def list_schedules(db, *, backend=None) -> list[dict]:
    """Every scheduled thing, each row TYPE-TAGGED.

    OS schedules (``type='os'``) from the backend's ``list_tasks()``; loops
    (``type='loop'``) from ``list_active_loops()``. Pure — the caller renders."""
    rows: list[dict] = []
    for t in _os_tasks(_get_backend(backend), strict=False):
        rows.append(
            {"type": "os", "id": t.label, "schedule": t.schedule, "status": t.status}
        )
    for lp in db.list_active_loops():
        rows.append(
            {
                "type": "loop",
                "id": lp["id"],
                "schedule": lp["cadence"],
                "status": lp["status"],
                "project_id": lp["project_id"],
            }
        )
    return rows


def delete_schedule(db, sched_id: str, *, purge: bool = False, backend=None) -> dict:
    """Delete a scheduled thing by id, TYPE-DISPATCHED. Fail loud on unknown id.

    LOOP (``get_loop`` resolves the id):
      - default = SOFT: ``pause_loop`` + project-close (recoverable).
      - ``--purge`` = HARD: close the project + ``delete_loop`` (row removed).
    OS SCHEDULE (id is a known backend label): ``uninstall(label)``.
    Neither → ``ValueError`` (never a silent no-op)."""
    loop = db.get_loop(sched_id)
    if loop is not None:
        project_id = loop["project_id"]
        if purge:
            # --purge hard-removes the loop and EVERYTHING it owns (nodes across
            # every generation, their edges, the project row, and loop-owned
            # ledger rows) in ONE atomic transaction — see dbops.loops.purge_loop.
            db.purge_loop(sched_id)
            return {"type": "loop", "action": "purge", "id": sched_id}
        db.pause_loop(sched_id)
        db.close_project(
            project_id, f"Loop {sched_id} deleted (schedule:delete, soft)", {}
        )
        return {"type": "loop", "action": "soft", "id": sched_id}

    # OS branch — strict=True: a backend that fails to ENUMERATE surfaces its real
    # error here rather than letting a still-installed schedule fall through to a
    # misleading "unknown id" no-op (Important-1, code review 2026-07-04).
    backend = _get_backend(backend)
    if backend is not None and sched_id in {t.label for t in _os_tasks(backend, strict=True)}:
        backend.uninstall(sched_id)
        return {"type": "os", "action": "uninstall", "id": sched_id}

    raise ValueError(
        f"unknown schedule id: {sched_id!r} — not an active loop or a known OS "
        "schedule. Run `juggle schedule list` to see valid ids."
    )


# ── CLI handlers (thin — the logic above is what tests exercise) ──────────────
def cmd_schedule_list(args):
    rows = list_schedules(get_db())
    if not rows:
        print("No scheduled tasks or loops.")
        return
    for r in rows:
        tag = "OS  " if r["type"] == "os" else "LOOP"
        print(f"[{tag}] {r['id']:<28} {r.get('schedule', ''):<16} {r.get('status', '')}")


def cmd_schedule_delete(args):
    res = delete_schedule(get_db(), args.id, purge=args.purge)
    if res["type"] == "os":
        print(f"Uninstalled OS schedule {res['id']}.")
    elif res["action"] == "purge":
        print(f"Purged loop {res['id']} (row removed; non-recoverable).")
    else:
        print(
            f"Deleted loop {res['id']} (paused + project closed; "
            "restore via /juggle:project:open)."
        )
