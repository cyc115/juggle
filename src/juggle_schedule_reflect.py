"""Deprecated shim — module moved to schedules.reflect (2026-06-10 Phase 3).

Kept so external callers using the old flat path keep working; new code must
import from schedules.reflect.
"""

from schedules.reflect import *  # noqa: F401,F403
