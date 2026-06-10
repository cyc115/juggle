"""Deprecated shim — module moved to schedules.dogfood (2026-06-10 Phase 3).

Kept so external callers using the old flat path keep working; new code must
import from schedules.dogfood.
"""

from schedules.dogfood import *  # noqa: F401,F403
