"""Deprecated shim — module moved to schedules.autofix (2026-06-10 Phase 3).

Kept so external callers using the old flat path keep working; new code must
import from schedules.autofix.
"""

from schedules.autofix import *  # noqa: F401,F403
