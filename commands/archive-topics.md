---
description: Archive completed or stale conversation topics to reduce clutter
allowed-tools: Bash
---

# /juggle:archive-topics — Archive Completed or Stale Topics

When the user runs `/juggle:archive-topics`, scan for threads ready to be archived and let the user confirm before archiving anything.

## Step 1: Get archive candidates

Run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-archive-candidates
```

If the output is `No archive candidates.` — respond:
> "Nothing to archive right now."

Stop here.

## Step 2: Present candidates

Format the output as a list with status badge and last_active for each candidate. For example:

```
Found N topics ready to archive:

  [C] Battery usage check      ✅ done — completed 1 hr ago
  [B] Juggle architecture research  ✅ done — completed 2 hrs ago
  [A] General                  💤 idle — no activity for 9 hours

Archive all N? (yes / pick / skip)
  yes   — archive all
  pick  — choose individually
  skip  — do nothing
```

Wait for the user's response.

## Step 3: Handle response

### "yes" — archive all candidates

For each candidate thread, run:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py archive-thread <tid>
```

Report what was archived:
```
Archived N topics: [C] Battery usage check, [B] Juggle architecture research, [A] General
```

### "pick" — choose individually

For each candidate in order, ask:
```
Archive [C] Battery usage check? (y/n)
```

Wait for the user's answer before moving to the next. Archive confirmed threads one at a time:
```bash
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py archive-thread <tid>
```

At the end, report which threads were archived and which were skipped.

### "skip" — do nothing

Respond:
> "No changes made."

## Error handling

If any `archive-thread` command errors, report the error and continue with the remaining threads. Do not abort the whole operation on a single failure.

If juggle is not active or the CLI errors on `get-archive-candidates`:
> "Juggle mode isn't active. Run /juggle:start to activate it."
