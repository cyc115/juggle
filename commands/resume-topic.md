---
description: Switch to a specific conversation topic by ID
allowed-tools: Bash, Agent, Edit, Write
---

# /juggle:resume-topic — Switch Conversation Topic

When the user runs `/juggle:resume-topic <id>`, switch the foreground conversation to the specified topic.

## Arguments
- `<id>` — The topic letter (A, B, C, D). Required.

## What to Do

1. **Save the current topic's state** before switching. Generate a rolling summary of the conversation so far, then persist it:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <current_id> "<summary>"
   ```
   Also save key decisions and open questions as they arise:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-meta <current_id> --add-decision "<text>"
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-meta <current_id> --add-question "<text>"
   ```

2. **Load the target topic from the backend**:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py switch-thread <id>
   ```
   If this errors with "Thread not found", respond:
   `"Topic [X] doesn't exist. Use /juggle:show-topics to see open topics."`

3. **Present the loaded context** to the user:
   ```
   Returning to Topic [X]: [label]

   Where we left off: [summary from CLI output]

   Key decisions:
   - [from CLI output]

   Open questions:
   - [from CLI output]

   [If background agent completed:]
   Background results: [agent_result from CLI output]
   ```

4. **If the topic has a completed background agent result**: Present the results and ask if the user wants to discuss them or move on.

5. **If the topic has a failed background agent**: Show the error reason and ask if the user wants to retry.

6. **Offer to send the previous topic to background** if it had ongoing work:
   `"Want me to continue working on Topic [previous] in the background while you're here?"`
