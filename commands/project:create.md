---
name: project:create
description: Create a new Juggle project (interactive coach or --force non-interactive)
allowed-tools: Bash
---

# /juggle:project:create — Create a Juggle Project

Create a new Juggle project. Accepts optional args: `--name "Name" --objective "Objective"`.

## Execution

If `$ARGUMENTS` contains `--name` and `--objective`, run non-interactive:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project create --force $ARGUMENTS
```

Otherwise, run the interactive LLM coach wizard:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project create
```

## After creation

Run `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py project list` and report:
- New project ID and name
- Objective (truncated to 1 line)
- Total project count
