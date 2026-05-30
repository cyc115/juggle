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
