---
description: Archive completed or stale conversation topics to reduce clutter
allowed-tools: Bash
---

# /juggle:archive-topics — Archive Topics

## Step 1: Get candidates

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-archive-candidates
```

On `No archive candidates.`:
> "Nothing to archive."

Stop.

## Step 2: Present candidates

```
Found N topics ready to archive:

  [C] Battery usage check          ✅ done — 1 hr ago
  [B] Juggle architecture research ✅ done — 2 hrs ago
  [A] General                      💤 idle — 9 hrs ago

Archive all N? (yes / pick / skip)
  yes  — archive all
  pick — choose individually
  skip — do nothing
```

Wait for response.

## Step 3: Handle response

### "yes"

For each candidate:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py archive-thread <tid>
```

Report:
```
Archived N topics: [C] Battery usage check, [B] Juggle research, [A] General
```

### "pick"

For each candidate:
```
Archive [C] Battery usage check? (y/n)
```

Wait before next. Archive confirmed threads:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py archive-thread <tid>
```

Report archived and skipped.

### "skip"

> "No changes made."

## Error handling

On `archive-thread` error: report and continue. Don't abort.

On CLI error or juggle not active:
> "Juggle not active. Run /juggle:start."
