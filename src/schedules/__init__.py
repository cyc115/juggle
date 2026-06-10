"""
schedules — Scheduled routine package (autofix / dogfood / reflect + common).

Owns: weekly scheduled routines invoked via `juggle schedule-<name>` and their
shared helpers (state file, gh wrappers, claude -p cost tracking).
Must not own: the launchd/systemd backend (juggle_scheduler.py) or CLI wiring.
"""
