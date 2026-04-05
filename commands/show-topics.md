---
description: Show all open conversation topics with status
allowed-tools: Bash
---

# /juggle:show-topics — Display Open Topics

When the user runs `/juggle:show-topics`, display the current state of all open conversation topics.

## What to Do

1. **Read the live state from the backend**:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py show-topics
   ```

2. **If the output says "No topics."** or the command errors with juggle not active, respond:
   `"Juggle mode isn't active. Run /juggle:start to activate it."`

3. **Display the output** from the CLI, formatted like:
   ```
   Topics:
     [A] Auth module design        <- you are here
     [B] API rate limiting         -> agent running...
     [C] Quick Q: env var config   done (results ready)
   ```

4. **After the table**, show a brief reminder:
   ```
   Use "/juggle:resume-topic <id>" to switch topics, or just keep talking.
   ```
