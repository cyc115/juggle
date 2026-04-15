---
name: next-action
description: Pick and resume the highest-priority action item from the cockpit
---

Run:
```bash
python3 /Users/mikechen/github/juggle/src/juggle_cli.py next-action
```

This switches to the topic with the most urgent action item (blocker > review > idle open question > any open question). Read the output and present the briefing to the user.
