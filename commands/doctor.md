---
description: Migrate Juggle config + DB to the current schema (one-shot upgrade helper).
allowed-tools: Bash
---

# /juggle:doctor — Config + DB Migration

Runs the `juggle doctor` CLI which:

1. Backs up `~/.juggle/config.json` to `~/.juggle/config.json.bak-pre-1.21`.
2. Rewrites the config to move `domains.initial_domain_paths` (vault entry) into `paths.vault`, and `domains.vault_name` into `paths.vault_name`.
3. Removes the obsolete `domains` block.
4. Runs Migrations 17–19 on `~/.claude/juggle/juggle.db`, dropping `threads.domain`, `agents.domain`, and the `domains` / `domain_paths` tables.

## Run

```bash
uv run ~/github/juggle/src/juggle_cli.py doctor
```

For a preview without writes:

```bash
uv run ~/github/juggle/src/juggle_cli.py doctor --dry-run
```

Report the output. If the user wants to revert, restore `~/.juggle/config.json.bak-pre-1.21` and downgrade Juggle to a 1.20.x release.
