---
description: Search research KB — HN articles, PDFs, vault, memory, and web
allowed-tools: Bash, mcp__web-search__search-web
---

# /juggle:research — Research Knowledge Base

Search for a topic across HN articles, PDFs, vault notes, Hindsight memory, and the web.

**Usage:** `/juggle:research <topic> [--verbose] [--no-web]`

## Steps

### 1. Check web search config

```bash
source ~/.juggle/.env 2>/dev/null; true
python3 -c "
import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_settings import get_settings
print(get_settings()['research_kb'].get('web_search_enabled', True))
"
```

### 2. If web search enabled (and `--no-web` not passed), run web search

Use `mcp__web-search__search-web` with the topic as query. Collect all results into a JSON array:
```json
[{"title": "...", "url": "...", "snippet": "..."}]
```
Store as `WEB_JSON` (serialized single-line JSON string). If web search disabled or `--no-web` passed, set `WEB_JSON=""`.

### 3. Run research command

```bash
source ~/.juggle/.env 2>/dev/null; true
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cmd_research.py "<TOPIC>" \
  ${VERBOSE_FLAG} \
  ${WEB_JSON:+--web-results "$WEB_JSON"}
```

Set `VERBOSE_FLAG=--verbose` if user passed `--verbose`, otherwise leave empty.

Print the script's stdout output directly to the user.

### 4. If DB not initialized

If the script exits with an error about a missing DB or missing table, tell the user:
> "Research KB not initialized. Run `/juggle:init` first, then `/juggle:research-ingest` to populate the HN corpus."
