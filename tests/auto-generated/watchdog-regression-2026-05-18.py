I need the complete event data to generate meaningful tests. The `watchdog_events` table has several fields beyond just `id`. Can you provide:

1. **Full event data** — the complete `watchdog_events` row(s) with `agent_id`, `event_type`, `snapshot_path`, `created_at`, etc.?
2. **Or the source** — is this from `juggle.db`, a log file, or another source? I can query it directly.
3. **Test scope** — which event types should I cover? (e.g., just `stalled`, or all types: `stalled`, `crashed`, `recovered`, `retry_blocked`, `prompt_resolved`?)

For context, the watchdog spec shows these event types with different recovery paths:
- **stalled_silent** → aggressive re-dispatch
- **crashed** → mark failed + re-dispatch
- **stuck_at_prompt** → send Enter (with escalation if it fails)
- **recoverable_prompt** → auto-send safe key
- **orphaned** → file action item (no recovery in v1)

Once you clarify, I'll generate regression tests that reproduce each stall condition and verify the recovery path.