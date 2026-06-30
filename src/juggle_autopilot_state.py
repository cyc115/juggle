"""juggle_autopilot_state — autopilot settings accessors (default-armed model).

2026-06-30 (user-approved restore): per-project arming is BACK with a
DEFAULT-ARMED semantic. The stored authority is a DISARMED *exclusion* set
(``DISARMED_PROJECT_KEY``); an empty set means every active project is armed,
which is behaviour-identical to the P7 "tick drives all active" model.

``select_armed`` is the ONE source of truth for "active minus disarmed" — the
tick, the hooks carve-out, the CLI, and the cockpit overlay all route through
it. ``ARMED_PROJECT_KEY`` is retained as a legacy constant for back-compat
imports only; nothing reads it for behaviour (D2: a stale value is ignored).
Must not own: dispatching, scheduling, or the CLI surface.
"""

from __future__ import annotations

ARMED_PROJECT_KEY = "autopilot_armed_project"          # LEGACY (dead) — back-compat import only
DISARMED_PROJECT_KEY = "autopilot_disarmed_project"    # authority: the exclusion set


def _validate(pid: str) -> None:
    if not pid or pid != pid.strip() or "," in pid or any(c.isspace() for c in pid):
        raise ValueError(
            f"project id {pid!r} is not a valid disarmed-set member "
            "(no commas/whitespace — ids are slugs)"
        )


def get_disarmed_projects(db) -> list[str]:
    """Ordered, deduped disarmed project ids; [] when unset/blank/pre-migration."""
    try:
        raw = db.get_setting(DISARMED_PROJECT_KEY) or ""
    except Exception:
        return []  # pre-migration DB without a settings table
    out: list[str] = []
    for part in raw.split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def set_disarmed_projects(db, pids: list[str]) -> None:
    """Persist the exclusion set; empty list clears the key (all armed)."""
    db.set_setting(DISARMED_PROJECT_KEY, ",".join(pids) if pids else None)


def select_armed(all_ids: list[str], disarmed) -> list[str]:
    """PURE: order-preserving ``all_ids`` minus the ``disarmed`` exclusion set.

    The single source of truth for the default-armed rule. Empty ``disarmed``
    returns ``all_ids`` unchanged (back-compat: every project driven).
    """
    ds = set(disarmed)
    return [pid for pid in all_ids if pid not in ds]


def disarm_project(db, pid: str) -> list[str]:
    """Add ``pid`` to the disarmed set (idempotent); returns the new set."""
    _validate(pid)
    current = get_disarmed_projects(db)
    if pid not in current:
        current.append(pid)
    set_disarmed_projects(db, current)
    return current


def arm_project(db, pid: str) -> list[str]:
    """Remove ``pid`` from the disarmed set (re-arm; no-op if already armed)."""
    _validate(pid)
    current = [p for p in get_disarmed_projects(db) if p != pid]
    set_disarmed_projects(db, current)
    return current


def arm_all(db) -> None:
    """Clear the disarmed set — every project armed."""
    set_disarmed_projects(db, [])


def disarm_all(db, all_ids: list[str]) -> None:
    """Disarm every id in ``all_ids`` (exclude the whole active set)."""
    out: list[str] = []
    for pid in all_ids:
        _validate(pid)
        if pid not in out:
            out.append(pid)
    set_disarmed_projects(db, out)


def _active_ids(db) -> list[str]:
    """Active (non-archived/closed) project ids; [] on any DB error."""
    try:
        return [p["id"] for p in db.list_projects()]
    except Exception:
        return []


def get_armed_projects(db) -> list[str]:
    """DERIVED armed set = active project ids minus the disarmed exclusion set."""
    return select_armed(_active_ids(db), get_disarmed_projects(db))


def get_armed_project(db) -> str | None:
    """COMPAT SHIM: first derived-armed project or None (legacy callers)."""
    armed = get_armed_projects(db)
    return armed[0] if armed else None
