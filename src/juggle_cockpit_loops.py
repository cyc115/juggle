"""Juggle Cockpit Loops — loop-entity → cockpit render-band adapter (V2 §4/§8).

Owns the ``LoopRow`` snapshot type and the pure helpers that turn a loop's DB
columns into the Agents-panel 'Loops' sub-section: the identity glyph, the
last-outcome badge, the ▸Pn project pointer, one combined display line per loop,
and the at-scale collapse-to-summary (§8). Mirrors juggle_cockpit_sched's shape
(a frozen dataclass + a ``fetch_*`` DB reader). Zero Rich imports — the view
wraps the returned STRINGS in Text, so no glyph literal ever lives in a renderer.

Badge rule (spec §4.1): there is NO ``loops.last_status`` column, so the badge is
derived from EXISTING columns — ``consecutive_failures > 0`` → ⚠n; else
``last_run_at`` set → ✓<run_seq>; else never-run → ·. The ▸Pn pointer is the
loop's own kind='loop' PROJECT (bridges to its graph WITHOUT claiming a P-slot,
exclusion enforced in the snapshot projects query), never the INBOX
conversation-thread mirror node.
"""
from __future__ import annotations

from dataclasses import dataclass

from juggle_cockpit_legend import (
    GRAPH_READY_SUFFIX,
    LOOP_FAIL_BADGE,
    LOOP_GLYPH,
    LOOP_NEVER_BADGE,
    LOOP_OK_BADGE,
    LOOP_PAUSED_GLYPH,
)

# At/above this many loops the band collapses to top rows + a summary (§8):
# dozens of standing loops must not render one-row-each unconditionally.
_LOOP_COLLAPSE_LIMIT = 6


@dataclass(frozen=True)
class LoopRow:
    loop_id: str            # "L3"
    name: str               # loop project name (or loop_id fallback)
    cadence: str            # raw cadence, e.g. "every 6h"
    next_run: str | None    # ISO next-fire timestamp, or None
    project_id: str         # "P8" — the ▸Pn pointer target (loop's kind='loop' project)
    status: str             # "active" | "paused"
    consecutive_failures: int
    run_seq: int
    last_run_at: str | None


def loop_badge(consecutive_failures: int | None, last_run_at, run_seq: int | None) -> str:
    """Last-outcome badge from existing columns only (no last_status column)."""
    if consecutive_failures and consecutive_failures > 0:
        return f"{LOOP_FAIL_BADGE}{consecutive_failures}"
    if last_run_at:
        return f"{LOOP_OK_BADGE}{run_seq or 0}"
    return LOOP_NEVER_BADGE


def loop_glyph(status: str) -> str:
    """Identity glyph: 🔁 for an active loop, the paused glyph when broken."""
    return LOOP_PAUSED_GLYPH if status == "paused" else LOOP_GLYPH


def loop_pointer(project_id: str) -> str:
    """▸Pn pointer at the loop's own project (never the INBOX mirror)."""
    return f"{GRAPH_READY_SUFFIX}{project_id}" if project_id else ""


def _fmt_cadence(cadence: str) -> str:
    from juggle_loop_cadence import format_weekly

    wk = format_weekly(cadence)  # "Mon 09:00" for a weekly cadence, else None
    if wk:
        return wk
    s = (cadence or "").replace("every ", "").replace("daily at ", "daily ").strip()
    return s


def _fmt_next(next_run) -> str:
    """Compact next-fire time '→HH:MM' from an ISO next_run (best-effort).

    Rendered LAST in the line so a narrow pane crops it first (the load-bearing
    badge/pointer survive). Empty string when unparseable / unset."""
    if not next_run:
        return ""
    s = str(next_run)
    sep = "T" if "T" in s else " "
    if sep in s:
        hhmm = s.split(sep, 1)[1][:5]
        if len(hhmm) == 5 and hhmm[2] == ":":
            return f"→{hhmm}"
    return ""


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _needs_attention(row: LoopRow) -> bool:
    """A paused (circuit-broken) or failing loop — must never be the one the
    collapse HIDES (triage-first, mirrors the Agents pane busy>stale>idle sort)."""
    return row.status == "paused" or bool(
        row.consecutive_failures and row.consecutive_failures > 0
    )


def loop_line(row: LoopRow, name_width: int = 16) -> str:
    """One compact combined display line for a loop row.

    Layout: ``glyph name cadence ▸Pn badge →next``. The view renders this inside
    an ellipsis-cropping cell, so it can never overflow the (narrow) Agents pane;
    ordering keeps the load-bearing badge/pointer ahead of the croppable tail.
    """
    parts = [loop_glyph(row.status), _trunc(row.name, name_width)]
    cad = _fmt_cadence(row.cadence)
    if cad:
        parts.append(cad)
    ptr = loop_pointer(row.project_id)
    if ptr:
        parts.append(ptr)
    parts.append(loop_badge(row.consecutive_failures, row.last_run_at, row.run_seq))
    nxt = _fmt_next(row.next_run)
    if nxt:
        parts.append(nxt)
    return " ".join(parts)


def collapse_loop_lines(rows: list[LoopRow], limit: int = _LOOP_COLLAPSE_LIMIT) -> list[str]:
    """Return the loop band's display lines, collapsing to a summary at scale.

    Rows are ordered attention-first (paused/failing ahead of healthy, stable
    within a group) so the collapse never HIDES a broken loop behind a healthy
    one. ≤ ``limit`` loops → one line each. Above it → the first ``limit-1`` rows
    plus a summary line ('🔁 +N more (A active, P paused, F failing)') so the
    pane stays bounded no matter how many standing loops exist (§8)."""
    rows = sorted(rows, key=lambda r: 0 if _needs_attention(r) else 1)
    if len(rows) <= limit:
        return [loop_line(r) for r in rows]
    shown = rows[: limit - 1]
    hidden = rows[limit - 1:]
    active = sum(1 for r in hidden if r.status != "paused")
    paused = sum(1 for r in hidden if r.status == "paused")
    failing = sum(1 for r in hidden if r.consecutive_failures and r.consecutive_failures > 0)
    lines = [loop_line(r) for r in shown]
    lines.append(
        f"{LOOP_GLYPH} +{len(hidden)} more "
        f"({active} active, {paused} paused, {failing} failing)"
    )
    return lines


def fetch_loops(db) -> list[LoopRow]:
    """Read the render band's loops (active + paused) from the DB.

    Resolves each loop's display name from its kind='loop' project. Graceful on
    a DB without the loops table / list_loops (returns [])."""
    try:
        raw = db.list_loops()
    except Exception:
        return []
    names: dict[str, str] = {}
    try:
        with db._connect() as conn:
            for r in conn.execute("SELECT id, name FROM projects").fetchall():
                names[r[0]] = r[1]
    except Exception:
        names = {}
    out: list[LoopRow] = []
    for loop in raw:
        pid = loop.get("project_id") or ""
        out.append(
            LoopRow(
                loop_id=loop.get("id") or "?",
                name=names.get(pid) or loop.get("id") or "loop",
                cadence=loop.get("cadence") or "",
                next_run=loop.get("next_run"),
                project_id=pid,
                status=loop.get("status") or "active",
                consecutive_failures=loop.get("consecutive_failures") or 0,
                run_seq=loop.get("run_seq") or 0,
                last_run_at=loop.get("last_run_at"),
            )
        )
    return out
