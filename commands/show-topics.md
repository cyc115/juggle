---
description: Show all open conversation topics with status
allowed-tools: Bash, Agent
---

# /juggle:show-topics — Display Open Topics

When the user runs `/juggle:show-topics`:

## Step 1: Sweep stale summaries

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-stale-threads
```

For each thread listed as stale, spawn a Haiku background summarizer agent using the
template from `commands/start.md § Auto-Summary`. Wait for all to complete before
rendering — they are fast (single DB read, ~100 token output).

If no stale threads, proceed directly to Step 2.

## Step 2: Render topics

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py show-topics
```

Print the output **verbatim** — do not reformat or summarize it.

## Step 3: Reminder

After the output:
```
Use "/juggle:resume-topic <id>" to switch topics, or just keep talking.
```

## Error handling

If the command errors or outputs "No topics." / juggle not active:
> "Juggle mode isn't active. Run /juggle:start to activate it."
