---
description: Search the offline research KB only (no web, no vault, no synthesis)
allowed-tools: Bash
---

Search the local research KB (sqlite-vec + FTS5). No web, vault, Hindsight, or LLM synthesis.

Parse the user's request for these flags and pass them through verbatim:
- `--fts` — FTS-only mode, fully offline (no embedding API call, no OPENROUTER_KEY needed)
- `--json` — emit raw JSON instead of formatted list
- `-k N` / `--limit N` — max results (default 10)

Run the search:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_search_offline.py <ARGUMENTS>
```

Print all output to the user.

**Note:** If results are empty, run `/juggle:research-ingest` to populate the KB first.
