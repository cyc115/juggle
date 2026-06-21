"""juggle_autopilot_state — autopilot settings accessors.

P7: per-project arming is REMOVED. The ``autopilot_armed_project`` settings
key and its CSV format are preserved as dead data for backward compat (existing
DBs may have the key; reads are safe no-ops). ``arm_project`` and
``disarm_project`` raise DeprecationWarning and are no-ops — call sites in
the cockpit modals are the only remaining callers and they are guarded.
``get_armed_projects`` / ``get_armed_project`` remain for compat imports but
always return [] / None (arming is gone).
Must not own: dispatching, scheduling, or the CLI surface.
"""

from __future__ import annotations

ARMED_PROJECT_KEY = "autopilot_armed_project"


def get_armed_projects(db) -> list[str]:
    """Ordered, deduped armed project ids; [] when disarmed or pre-migration."""
    try:
        raw = db.get_setting(ARMED_PROJECT_KEY) or ""
    except Exception:
        return []  # pre-migration DB without a settings table
    out: list[str] = []
    for part in raw.split(","):
        pid = part.strip()
        if pid and pid not in out:
            out.append(pid)
    return out


def set_armed_projects(db, pids: list[str]) -> None:
    """Persist the set; empty list clears the key (disarmed)."""
    db.set_setting(ARMED_PROJECT_KEY, ",".join(pids) if pids else None)


def _validate(pid: str) -> None:
    if not pid or pid != pid.strip() or "," in pid or any(c.isspace() for c in pid):
        raise ValueError(
            f"project id {pid!r} is not a valid armed-set member "
            "(no commas/whitespace — ids are slugs)"
        )


def arm_project(db, pid: str) -> list[str]:
    """REMOVED (P7): per-project arming is gone. Raises RuntimeError."""
    raise RuntimeError(
        "Per-project arming is removed (P7). The tick dispatches all projects "
        "automatically. Use `juggle autopilot on/off` for the global toggle."
    )


def disarm_project(db, pid: str) -> list[str]:
    """REMOVED (P7): per-project arming is gone. Raises RuntimeError."""
    raise RuntimeError(
        "Per-project arming is removed (P7). The tick dispatches all projects "
        "automatically. Use `juggle autopilot on/off` for the global toggle."
    )


def get_armed_project(db) -> str | None:
    """COMPAT SHIM: first armed project or None (legacy single-armed callers)."""
    armed = get_armed_projects(db)
    return armed[0] if armed else None
