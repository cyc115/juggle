---
description: Ingest HN articles and PDFs into the research knowledge base
allowed-tools: Bash
---

# /juggle:research-ingest — Populate Research KB

Ingest HN articles from BigQuery and/or PDFs from configured directories.

**Usage:** `/juggle:research-ingest` or `/juggle:research-ingest --pdf-only`

## Prerequisites

- `bq` CLI installed and authenticated (`bq version` should work)
- `OPENROUTER_KEY` set in `~/.juggle/.env`
- Research KB initialized (run `/juggle:init` first)

## Steps

```bash
source ~/.juggle/.env 2>/dev/null; true
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_research_ingest.py <ARGUMENTS>
```

Pass `--pdf-only` if the user specified it; otherwise run with no extra args (ingests both HN and PDFs).

Print all output to the user. Ingest may take several minutes for large corpora.
