---
description: Dismiss open action items for one or more topics
allowed-tools: Bash
---

# /juggle:action-item:dismiss — Dismiss Topic Action Items

## Arguments

`$ARGUMENTS` — one or more topic IDs (e.g. `FK FL` or `A B`). Required.

## Steps

### 1. Parse topic IDs

Split `$ARGUMENTS` on whitespace. Each token is a topic ID (case-insensitive). If no arguments, print:
> "Usage: /juggle:action-item:dismiss <topic-id> [topic-id ...]"

and stop.

### 2. List open action items

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py list-actions
```

Output format per line: `⚡ [<id>] <tier> <message> (thread [<topic-id>])`

### 3. Match and dismiss

For each topic ID in the arguments:
- Find all lines where `(thread [<TOPIC-ID>])` matches (case-insensitive)
- Extract the numeric `<id>` from `[<id>]`
- For each matched action item, run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py ack-action <id>
```

### 4. Report

Print a one-line summary per topic:
- Found matches: `[FK] dismissed 2 action item(s): #39, #40`
- No matches: `[XY] no open action items found`
