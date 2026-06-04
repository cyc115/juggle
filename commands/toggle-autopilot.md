---
name: toggle-autopilot
description: Toggle autonomous development mode on/off. When ON, hand off a list of features/requirements and Claude drives each to the end goal — spec, decide, implement, verify, self-unblock — without pausing for approval.
---

# /juggle:toggle-autopilot

The flag below (`~/.juggle/autopilot`) is read by the `UserPromptSubmit` hook
(`src/juggle_hooks.py`), which re-injects the autopilot directive on every turn
while it's set — so the mode persists instead of being forgotten after this
command scrolls out of context.

Flip the flag, then act on the new state:

```bash
F="$HOME/.juggle/autopilot"; if [ -f "$F" ]; then rm "$F"; echo "AUTOPILOT OFF"; else mkdir -p "$HOME/.juggle" && touch "$F"; echo "AUTOPILOT ON"; fi
```

**OFF →** resume normal interactive mode (use AskUserQuestion for genuine decisions). Stop.

**ON →** adopt this loop until toggled off. If the user hasn't given a feature/requirements list, ask for one, then drive it to done autonomously:

1. Per feature: brainstorm/spec → devil's-advocate critique → resolve every open question yourself at staff level. Don't ask the user; decide, note why, proceed.
2. Implement on a feature branch via dispatched agents (TDD). After each feature, verify with a harness (drive it, assert the effect) before starting the next.
3. Self-unblock: on a blocker or stalled/hung agent, diagnose → recover (nudge or force-finalize the agent, research the blocker, choose the best option) → continue. Escalate ONLY for: missing credentials, an irreversible/destructive external action, or a product-direction fork with no defensible default.
4. Self-improve: when a failure mode recurs, append a one-line rule under "Learned rules" so it can't recur.
5. Finish only when every feature is implemented and verified (or a true escalation hits). Report the outcome.

## Learned rules
<!-- Claude appends concise, durable rules here as it hits and fixes issues. -->
- **send-task can silently fail to submit:** if the target pane's editor is in `-- VISUAL --` (or any non-insert) mode, the pasted prompt sits in the input and Enter is swallowed — the agent shows "busy"/stalled but never runs. After every send-task, capture the pane; if it didn't enter a running state, send `Escape` + `C-c` + `C-u` to reset to insert mode, then re-send. Prefer reusing a warm idle agent over a cold spawn (cold spawns get reaped mid-boot).
- **send-task may paste-without-submit even outside VISUAL mode:** symptom is a collapsed "paste again to expand" block in the input with the agent idle ("nap"/"yawn", not "Beaming/Thinking"). Recovery: `tmux send-keys -t <pane> Enter` to submit the already-queued task — only do the full Escape/C-c/C-u reset if Enter doesn't start it running. ALWAYS capture the pane ~4s after send-task and confirm a running state ("Beaming/Thinking… Ns") before moving on.
