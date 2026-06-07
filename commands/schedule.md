---
description: Create a scheduled task that runs on a schedule and appears in the Juggle cockpit (macOS launchd / Linux systemd / cron)
allowed-tools: Bash, Read, Write
---

# /juggle:schedule

Create a recurring scheduled task using the platform-appropriate backend:
- **macOS** — launchd LaunchAgent (plist in `~/Library/LaunchAgents/`)
- **Linux (systemd)** — systemd user timer (units in `~/.config/systemd/user/`)
- **Linux (fallback)** — crontab entry

The task label is prefixed so it appears automatically in the Juggle cockpit's Pool section.

## Arguments

`$ARGUMENTS` — natural language instruction, e.g.:
- `run ~/github/trading-edge/scripts/news-ingest every 15 minutes`
- `news-ingest: ~/github/trading-edge/scripts/news-ingest every 15m`
- `run /path/to/script daily at 09:00`

## What to do

Parse `$ARGUMENTS` to extract:
1. **label** — short kebab-case name (e.g. `trading-edge-news-ingest`). Derive from script filename if not explicit.
2. **program** — absolute path to the script/binary (expand `~` to `/Users/mikechen`)
3. **interval** — one of:
   - `every Nm` / `every N minutes` → `StartInterval` = N×60
   - `every Nh` / `every N hours` → `StartInterval` = N×3600
   - `daily at HH:MM` → `StartCalendarInterval` with Hour+Minute
   - `every Ns` / `every N seconds` → `StartInterval` = N

Then:

### 0. Harden the script (REQUIRED — do this before writing the plist)

launchd runs with a restricted PATH that omits `~/.local/bin` where `claude`, `uv`, and
other user binaries live. Any script the plist calls MUST contain all three of the
following. If the script already exists, patch it; if you're writing it, include this
as the opening block:

```bash
# [1] PATH — prepend user dirs so launchd can find claude, uv, brew tools
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# [2] Absolute binary resolution with fallbacks (add one line per external binary)
CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
# Then invoke via: "$CLAUDE_BIN" -p ...; "$UV_BIN" run ...

# [3] Exit trap + startup log line (tee so BOTH internal log AND launchd log capture it)
LOG="/path/to/app.log"
_on_exit() { local rc=$?; echo "[$(date '+%Y-%m-%d %H:%M:%S')] EXIT rc=$rc" | tee -a "$LOG"; }
trap _on_exit EXIT
echo "[$(date '+%Y-%m-%d %H:%M:%S')] START" | tee -a "$LOG"
```

**Checklist before moving to step 1:**
- [ ] Script exports `PATH` with `$HOME/.local/bin` first
- [ ] Each external binary has a resolved `_BIN` var with `|| echo` fallback; invoked via `"$VAR"`
- [ ] EXIT trap fires on any exit (including early 127); START line uses `tee -a` not `>>`

### 1. Write the plist

Write to `/Users/mikechen/Library/LaunchAgents/me.mikechen.<label>.plist`:

\`\`\`xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>me.mikechen.LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>PROGRAM</string>
    </array>
    SCHEDULE_KEY
    <key>StandardOutPath</key>
    <string>/Users/mikechen/Library/Logs/me.mikechen.LABEL.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/mikechen/Library/Logs/me.mikechen.LABEL.log</string>
    <key>RunAtLoad</key>
    <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/mikechen/.local/bin:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>/Users/mikechen</string>
    </dict>
</dict>
</plist>
\`\`\`

Where \`SCHEDULE_KEY\` is either:
- \`<key>StartInterval</key><integer>N</integer>\` for interval-based
- \`<key>StartCalendarInterval</key><dict><key>Hour</key><integer>H</integer><key>Minute</key><integer>M</integer></dict>\` for daily

### 2. Load it

\`\`\`bash
launchctl unload ~/Library/LaunchAgents/me.mikechen.<label>.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/me.mikechen.<label>.plist
launchctl list me.mikechen.<label>
\`\`\`

### 3. Report

Print:
- Plist path
- Label: \`me.mikechen.<label>\`
- Schedule: human-readable (e.g. "every 15m", "daily 09:00")
- Log: path
- Status from \`launchctl list\`
- "Appears in Juggle cockpit as: \`<label>\`"

---

## Linux — systemd user timer

On Linux with systemd, run instead:

\`\`\`python
from juggle_scheduler import get_backend, ScheduleSpec
backend = get_backend()  # auto-selects SystemdUserBackend or CronBackend
backend.install(ScheduleSpec(
    label="<label>",
    program="<absolute-path-to-program>",
    interval_secs=<N>,        # every N seconds, OR
    calendar={"hour": H, "minute": M},  # daily at HH:MM
))
\`\`\`

Units are written to `~/.config/systemd/user/juggle-<label>.{service,timer}`.
Logs: `~/.juggle/logs/juggle-<label>.log`

**Important:** For tasks to fire when you are logged out, enable linger once:
\`\`\`bash
sudo loginctl enable-linger $USER
# Verify: loginctl show-user $USER | grep Linger
\`\`\`

To view logs: `journalctl --user -u juggle-<label> -f`

To remove: `python3 -c "from juggle_scheduler import get_backend; get_backend().uninstall('<label>')"`

---

## Linux — cron fallback

On Linux without systemd, `get_backend()` falls back to `CronBackend`, which manages entries in the user's crontab. Logs go to `~/.juggle/logs/<label>.log`.

**Note:** Cron has no missed-run catch-up — if the machine is off at the scheduled time, the run is skipped.
