---
name: juggle:project:synthesis
description: Re-synthesize match_profile for one or more projects via `project synth`.
---

Run `project synth` to refresh project match profiles.

Examples:
- `/juggle:project:synthesis` — synth all dirty projects
- `project synth P1` — synth a specific project
- `project synth --all` — force-synth all active projects

The CLI equivalent: `uv run src/juggle_cli.py project synth [--all|--dirty|<id>]`
