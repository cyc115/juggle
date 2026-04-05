---
description: Activate juggle mode — multi-topic conversation orchestrator for the current session
allowed-tools: Read, Glob, Grep, Bash, Agent, Edit, Write
---

# /juggle:start — Activate Multi-Topic Orchestrator

When the user runs `/juggle:start`, activate juggle mode for the rest of this conversation session.

## What to Do

1. **Initialize the backend** by running:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py start
   ```
   This initializes the SQLite database, registers hooks in `~/.claude/settings.json`, and marks juggle as active. Show the CLI output to the user.

2. **Acknowledge activation** with a brief confirmation:
   ```
   Juggle mode activated. I'll track conversation topics and manage background agents.

   You can:
   - Just talk normally — I'll detect topic changes and offer to create new threads
   - `/juggle:show-topics` — see all open topics
   - `/juggle:resume-topic <id>` — switch to a specific topic
   ```

3. **Initialize the orchestrator state** by maintaining the following in your working memory for the rest of the session:

   **Topic Registry** (track all open topics):
   ```
   Topic {
     id:              string (A, B, C, D — sequential letter)
     label:           string (short human-readable topic name)
     status:          active | background | done | failed
     summary:         string (rolling summary, updated on every switch-away)
     key_decisions:   string[] (structured list of decisions made)
     open_questions:  string[] (unresolved questions)
     last_user_intent: string (what the user was doing when they left this topic)
     agent_result:    string | null (concise result if background agent completed)
     last_active:     timestamp
   }
   ```

   **Shared Project Context** (available to all topics):
   - Project decisions that affect multiple topics
   - Architectural choices agreed upon in any thread
   - Key facts discovered that other topics should know

4. **Auto-create Topic A** from whatever the user discusses next. Label it based on the first substantive message. Create it in the backend:
   ```bash
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "<topic label>"
   ```

## Orchestrator Behavior (for the rest of the session)

### Topic Detection
On every user message, evaluate:
- **Continuation**: Message clearly relates to the current topic -> proceed normally
- **Topic shift**: Message introduces a substantially different subject -> ask:
  `"This seems like a new topic — start a new thread for '[detected topic]', or continue in Topic [current] ([current label])?"`
- **Bias toward continuation**: Only suggest a new topic if the shift is clear. Tangential remarks or brief asides should NOT trigger a new topic.

### Background Agent Dispatch
When a topic reaches a point where research, investigation, or code generation is needed:
- Offer: "I can work on this in the background while you move to something else. Want me to spin up an agent?"
- If user agrees: dispatch a background Agent with the task spec + relevant context, set topic status to `background`
- Prompt user: "Topic [X] is running in the background. What would you like to work on?"

### Completion Notifications
When a background agent finishes:
- Do NOT interrupt the current conversation
- Queue the notification and show it at the next natural pause:
  `[Topic B completed] API rate limiting analysis ready. Use "/juggle:resume-topic B" to view.`

### Topic Switching
When switching topics (via `/juggle:resume-topic` or implicit detection):
1. **Save current topic**: Generate/update rolling summary, key decisions, open questions, last user intent
2. **Load target topic**: Present summary to user:
   `"Returning to Topic [X]: [label]. Here's where we left off: [summary]. Key decisions: [list]. Open questions: [list]."`
3. Load last 3-5 messages from that topic for conversational continuity

### Summary Maintenance
- **On every switch-away**: Update the topic's rolling summary
- **Every ~10 exchanges in a topic**: Generate a segment summary to prevent unbounded growth
- **On background agent completion**: Capture results concisely in the topic summary
- Keep summaries compact (target <200 tokens each)

### Cross-Topic Awareness
When a decision in one topic affects the project broadly:
- Promote it to shared project context
- When relevant in another topic, mention: "Note: in Topic [X], we decided [decision]."

## Limits
- Max 4 concurrent topics. If exceeded, suggest closing a completed topic.
- Max 3 background agents simultaneously.
- Background agent timeout: 15 minutes.
