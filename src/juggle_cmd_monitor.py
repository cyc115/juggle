#!/usr/bin/env python3
"""Juggle — monitor cron-spec: the canonical CronCreate spec emitter.

Cron fallback for the Monitor tool (2026-07-07 legacy-monitor-cron plan): on
machines with telemetry disabled, /juggle:start can't stream agent events via
Monitor, so /juggle:doctor:enable-legacy-monitor arms a session CronCreate job
instead. This module keeps the cadence + poll prompt CODE-OWNED and DRY (one
source both the enable command and /juggle:start read) rather than duplicated
prose in commands/*.md.
"""

import json

from juggle_settings import get_settings

# The marker `[juggle legacy-monitor poll]` is how enable-legacy-monitor --off
# finds this job (CronList -> match -> CronDelete). Relay rules mirror the
# Monitor-tool contract documented in commands/start.md so a cron-driven poll
# reads identically to a live Monitor event.
POLL_PROMPT = (
    "[juggle legacy-monitor poll] Run the juggle one-shot monitor poll and "
    "relay each printed line to the user exactly as a live Monitor event "
    '(completion lines → "Review ready — [LABEL]: title" for researcher / '
    '"[LABEL] done — title" for coder/planner; every other kind as-is). '
    "If it prints nothing, output nothing — do NOT narrate an empty poll. "
    "Take no other action."
)


def cmd_monitor_cron_spec(args):
    """`juggle monitor cron-spec --json` — print {"cron", "prompt"} for CronCreate."""
    cadence = get_settings()["legacy_monitor"]["cadence"]
    print(json.dumps({"cron": cadence, "prompt": POLL_PROMPT}))
