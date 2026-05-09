---
name: open
description: Open a file in the persistent nvim server session
allowed-tools: Bash
---

# /juggle:open — Open File in nvim Server

Open a file in the nvim server running at `/tmp/juggle-nvim.sock`.

**Usage:** `/juggle:open <file>`

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py open-in-editor <file>
```

If the socket is not running, the command will print how to start one:
> Start nvim with: `nvim-juggle` (alias set up by `/juggle:init`)
