---
description: Create a launchd LaunchAgent that runs on a schedule and appears in the Juggle cockpit
allowed-tools: Bash, Read, Write
---

# /juggle:schedule

Create a macOS launchd LaunchAgent for a recurring task. The plist is named `me.mikechen.<label>` so it appears automatically in the Juggle cockpit's Pool section.

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
