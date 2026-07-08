---
description: Arm or disarm the cron fallback for the Monitor tool (telemetry-disabled machines).
allowed-tools: Bash, CronCreate, CronList, CronDelete
---

# /juggle:doctor:enable-legacy-monitor [--cadence "<cron>"] | --off

On machines with telemetry disabled, the Claude Code **Monitor** tool is
unavailable, so `/juggle:start` can't stream agent events. This command arms
a session `CronCreate` job that polls the same events on a cadence instead
(`juggle-agent-monitor --once`) — see `commands/start.md` for the
try-Monitor-then-cron-fallback arming logic this flag gates.

**Caveat (state this to the user on every run):** `CronCreate` jobs are
session-only, expire after 7 days, and only fire while the REPL is idle. This
command re-arms the flag so `/juggle:start` re-creates the job every session
— it is not a persistent system cron.

## Enable

```bash
CONFIG=~/.juggle/config.json
mkdir -p ~/.juggle
[ -f "$CONFIG" ] || echo '{}' > "$CONFIG"

# With --cadence "<cron>" given, also set legacy_monitor.cadence; otherwise
# only flip enabled (the config default cadence "*/5 * * * *" applies).
jq '.legacy_monitor.enabled = true' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
# jq --arg cadence "<cron>" '.legacy_monitor.enabled = true | .legacy_monitor.cadence = $cadence' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"

uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py monitor show-cron-spec --json
```

Take the `{"cron": ..., "prompt": ...}` JSON from `show-cron-spec --json` verbatim
and call `CronCreate` with that `cron` schedule and `prompt`. Do not
paraphrase or hand-write the prompt — it is code-owned so it stays identical
to what `/juggle:start`'s fallback would create.

Confirm to the user: "Legacy-monitor cron fallback enabled (cadence: `<cron>`).
Session-only, 7-day expiry, fires while idle — re-armed automatically each
`/juggle:start`."

## Disable (`--off`)

```bash
CONFIG=~/.juggle/config.json
[ -f "$CONFIG" ] && jq '.legacy_monitor.enabled = false' "$CONFIG" > "$CONFIG.tmp" && mv "$CONFIG.tmp" "$CONFIG"
```

Then `CronList` and find the job whose prompt contains the marker
`[juggle legacy-monitor poll]` — `CronDelete` it. If none is found, say so
(nothing to remove).

Confirm to the user: "Legacy-monitor cron fallback disabled and cron job
removed."
