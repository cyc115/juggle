"""juggle_loop_cadence — shared day-of-week (weekly) cadence parse + format helper.

ONE source of truth for the weekday grammar (rule-of-three: the parse site
``juggle_cmd_loop_create.compute_next_run`` and the two format sites
``juggle_cockpit_loops._fmt_cadence`` / ``juggle_loop_confirm_card`` all route
through here) so the weekday map + regex never triplicate and drift.

Grammar (case-insensitive):
  * ``weekly on <weekday> at HH:MM``   (primary)
  * ``every <weekday> at HH:MM``       (alias — the leading token is a weekday name,
    never a digit, so it cannot collide with the ``every N<unit>`` interval form)
  * <weekday> = full name OR 3-letter abbrev, ``Mon``=0 … ``Sun``=6 (datetime.weekday()).

Fail-loud (LoopTemplateError, never a bare ValueError traceback) on: an unknown
weekday in a ``weekly``-keyword string, an out-of-range time, or a ``weekly``-shaped
string missing the ``at HH:MM`` — a loop with no schedulable next_run would silently
never fire.
"""
from __future__ import annotations

import re

from juggle_loop_template_validator import LoopTemplateError

# name/abbrev → datetime.weekday() index (Mon=0 … Sun=6).
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# ``weekly on <wd> at HH:MM`` OR the ``every <wd> at HH:MM`` alias. The weekday token
# is letters-only (``[a-z]{3,9}``) so ``every 15m`` (digit-led) can never match here —
# but compute_next_run still orders this check BEFORE the interval regex to be safe.
_WEEKLY_RE = re.compile(
    r"(?:weekly\s+on|every)\s+(?P<wd>[a-z]{3,9})\s+at\s+(?P<h>\d{1,2}):(?P<m>\d{2})\b",
    re.IGNORECASE,
)
# A ``weekly``-keyword string that did NOT fully match above is malformed (bad weekday
# or missing time) — fail loud rather than silently falling through to 'unparseable'.
_WEEKLY_KW_RE = re.compile(r"\bweekly\b", re.IGNORECASE)


def parse_weekly(cadence: str) -> tuple[int, int, int] | None:
    """Parse a weekly cadence → ``(weekday, hh, mm)``, or ``None`` if not weekly-shaped.

    Raises ``LoopTemplateError`` on a malformed weekly cadence (unknown weekday under
    the ``weekly`` keyword, out-of-range time, or a ``weekly``-shaped string missing
    the time) — never a bare ValueError. An ``every <non-weekday>`` string is NOT
    weekly (returns ``None`` so the interval/daily parsers get their turn)."""
    s = cadence or ""
    m = _WEEKLY_RE.search(s)
    if m:
        wd = _WEEKDAYS.get(m.group("wd").lower())
        if wd is None:
            # 'weekly on <bad>' is a typo'd weekday → fail loud; 'every <word>' that
            # isn't a weekday is simply not a weekly cadence → let other parsers try.
            if _WEEKLY_KW_RE.search(s):
                raise LoopTemplateError(
                    f"unknown weekday in cadence {cadence!r} — expected "
                    f"monday|mon … sunday|sun"
                )
            return None
        hh, mm = int(m.group("h")), int(m.group("m"))
        if hh > 23 or mm > 59:
            raise LoopTemplateError(
                f"invalid time in cadence {cadence!r}: {hh:02d}:{mm:02d}"
            )
        return (wd, hh, mm)
    if _WEEKLY_KW_RE.search(s):
        raise LoopTemplateError(
            f"weekly cadence {cadence!r} must be 'weekly on <weekday> at HH:MM'"
        )
    return None


def format_weekly(cadence: str) -> str | None:
    """Compact human label for a weekly cadence (``'Mon 09:00'``), else ``None``.

    Display-only — swallows a malformed cadence to ``None`` (the renderer falls back
    to its raw/other format); the create/plan path is where a bad cadence fails loud."""
    try:
        wk = parse_weekly(cadence)
    except LoopTemplateError:
        return None
    if wk is None:
        return None
    wd, hh, mm = wk
    return f"{_WEEKDAY_ABBR[wd]} {hh:02d}:{mm:02d}"
