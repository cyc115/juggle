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

3. Run the CLI command. Print its output VERBATIM — do not reformat, reorder, or summarize it.
