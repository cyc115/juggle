# /juggle:search — Search + Filter

Search the web MCP and research KB, filter with Haiku, and report results.

**Usage:** `/juggle:search <query> [--no-web] [--no-kb] [-k N]`

- `--no-web` — skip web search
- `--no-kb` — skip KB
- `-k N` — KB result count (default 10)

## Steps

### 1. KB search

```bash
source ~/.juggle/.env 2>/dev/null; true
KB_JSON=$(python3 /Users/mikechen/github/juggle//src/juggle_cmd_search.py "<QUERY>" --no-web <EXTRA_FLAGS>)
echo "$KB_JSON"
```

Parse `kb` array from output. Each item has: `title`, `url`, `score`, `date`, `summary`.

### 2. Web search (skip if --no-web)

Call `mcp__web-search__search-web` with `query="<QUERY>"`.

Collect results as a JSON array: `[{"title": "...", "url": "...", "snippet": "..."}]`

### 3. Haiku filter pass

Pass both result sets to the filter script:

```bash
source ~/.juggle/.env 2>/dev/null; true
FILTERED=$(python3 /Users/mikechen/github/juggle//src/juggle_cmd_search.py "<QUERY>" \
  --no-kb --filter \
  --web-results '<WEB_RESULTS_JSON>')
echo "$FILTERED"
```

But since KB results are already in `$KB_JSON`, inject them by calling with both `--filter` and `--web-results`:

```bash
source ~/.juggle/.env 2>/dev/null; true
FILTERED=$(python3 /Users/mikechen/github/juggle//src/juggle_cmd_search.py "<QUERY>" \
  --filter \
  --web-results '<WEB_RESULTS_JSON>' \
  <EXTRA_FLAGS>)
echo "$FILTERED"
```

Parse `kb` and `web` arrays from `$FILTERED`.

### 4. Present results to user

Format and print as two sections:

**KB Results** (N hits after filter):
- [title](url) — reason

**Web Results** (N hits after filter):
- [title](url) — reason

Keep it tight — one line per result, no prose summary.
