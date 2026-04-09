---
description: Switch to a specific conversation topic by ID
allowed-tools: Bash, Agent, Edit, Write
---

# /juggle:resume-topic — Switch Conversation Topic

## Arguments
- `<id>` — Topic letter (A, B, C, D). Required.

## Steps

1. Save current topic state:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-summary <current_id> "<summary>"
   ```
   Save decisions/questions as they arise:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-meta <current_id> --add-decision "<text>"
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py update-meta <current_id> --add-question "<text>"
   ```

2. Load target topic:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py switch-thread <id>
   ```
   On "Thread not found":
   `"Topic [X] doesn't exist. Use /juggle:show-topics."`

3. Present loaded context:
   ```
   Returning to Topic [X]: [label]

   Where we left off: [summary]

   Key decisions:
   - [from CLI output]

   Open questions:
   - [from CLI output]

   [If background agent completed:]
   Background results: [agent_result]
   ```

4. Completed agent result: present results, ask to discuss or move on.

5. Failed agent: show error, ask to retry.

6. Offer to background prior topic if it had ongoing work:
   `"Continue Topic [previous] in background?"`
