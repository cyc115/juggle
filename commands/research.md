---
description: Search research KB — HN articles, PDFs, vault, memory, and web — via parallel background agents
allowed-tools: Bash
---

# /juggle:research — Research Knowledge Base

Delegates research to parallel background agents. Returns immediately; loops back with a report when done.

**Usage:** `/juggle:research <topic> [--no-web] [--verbose]`

## Steps

### 1. Parse arguments from `$ARGUMENTS`

- `TOPIC` — everything except flags (required)
- `NO_WEB` — true if `--no-web` present
- `VERBOSE` — true if `--verbose` present

Derive `SLUG` from TOPIC: first 4 words, lowercase, hyphens only (e.g. "claude for small business" → `claude-for-small`).

Derive `VAULT_PATH` by reading the vault domain from juggle settings (the domain path tagged `"vault"` in `domains.initial_domain_paths`), expanded with `$HOME`. Derive `REPORT_FILE`: `<VAULT_PATH>/research/YYYY-MM-DD-<SLUG>.md`.

### 2. Create thread

```bash
CREATE_OUT=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py create-thread "research-<SLUG>")
THREAD_LABEL=$(echo "$CREATE_OUT" | grep -oP '(?<=Created Topic )\w+')
echo "Thread: $THREAD_LABEL"
```

### 3. Get researcher agent

```bash
AGENT_INFO=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py get-agent "$THREAD_LABEL" --role researcher)
AGENT_ID=$(echo "$AGENT_INFO" | awk '{print $1}')
echo "Agent: $AGENT_ID"
```

If `get-agent` exits non-zero or prints "Agent pool full", tell the user and stop.

### 4. Write task file and dispatch

Fill in all `<PLACEHOLDERS>` with real values before writing the file. `VERBOSE_FLAG` is `--verbose` if user passed `--verbose`, otherwise empty. `NO_WEB_FLAG` is `--no-web` if user passed `--no-web`, otherwise empty.

```bash
TASK_FILE="/tmp/juggle_research_$(date +%s%N).txt"
cat > "$TASK_FILE" << 'TASKEOF'
[JUGGLE_THREAD:<THREAD_LABEL>]
Research topic: "<TOPIC>"

## Parallel research steps (run searches in parallel where possible):

1. Check web search config:
   source ~/.juggle/.env 2>/dev/null
   python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src'); from juggle_settings import get_settings; print(get_settings()['research_kb'].get('web_search_enabled', True))"

2. If web_search_enabled=True and --no-web was NOT requested:
   Use the mcp__web-search__search-web tool with query="<TOPIC>" depth="standard".
   Collect all results into a single-line JSON array:
   [{"title":"...","url":"...","snippet":"..."},...]
   Store as WEB_JSON.

3. Run the research script (searches KB, vault, memory, and web in parallel internally):
   source ~/.juggle/.env 2>/dev/null
   REPORT=$(python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cmd_research.py "<TOPIC>" <NO_WEB_FLAG> <VERBOSE_FLAG> ${WEB_JSON:+--web-results "$WEB_JSON"})

4. Save report to vault:
   VAULT_PATH=$(python3 -c "
import sys, os
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_settings import get_settings
paths = get_settings()['domains']['initial_domain_paths']
vault = next((p[0] for p in paths if p[1] == 'vault'), None)
print(os.path.expanduser('~') + vault if vault else '')
" 2>/dev/null)
   REPORT_FILE="${VAULT_PATH}/research/$(date +%Y-%m-%d)-<SLUG>.md"
   {
     printf "# Research: <TOPIC>\nDate: $(date +%Y-%m-%d)\n\n"
     echo "$REPORT"
   } > "$REPORT_FILE"
   echo "Saved: $REPORT_FILE"

5. Print the full report output so it appears in this thread.

6. Notify orchestrator and complete:
   ONE_LINE=$(echo "$REPORT" | head -5 | tr '\n' ' ' | cut -c1-200)
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <THREAD_LABEL> "Research complete: <TOPIC> — report at $REPORT_FILE" --type manual_step
   python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py complete-agent <THREAD_LABEL> "Research complete: $REPORT_FILE" --retain "$ONE_LINE"
TASKEOF

python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py send-task "$AGENT_ID" "$TASK_FILE"
```

### 5. Confirm dispatch

Tell the user:
> "Researching **<TOPIC>** in background — thread [<THREAD_LABEL>]. Report will be saved to the vault's `research/` directory and I'll loop back when done."
