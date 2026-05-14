# Design: `/juggle:research` — Personal Knowledge Base

**Date:** 2026-05-13  
**Status:** Approved

---

## Overview

A personal research knowledge base integrated into Juggle. The `/juggle:research [topic]` command performs parallel hybrid search across HN articles, local PDFs, vault notes, Hindsight memory, and the web, then synthesizes results via Gemini 3.1 Flash into a markdown digest with inline clickable links.

---

## Section 1: Data Layer

**Database:** `~/.juggle/research_kb.db` — dedicated SQLite file, separate from the main Juggle DB.

**Schema:**

```sql
-- Base table
CREATE TABLE articles (
    id       INTEGER PRIMARY KEY,
    title    TEXT NOT NULL,
    url      TEXT UNIQUE NOT NULL,
    score    INTEGER,
    date     TEXT,
    source   TEXT NOT NULL,  -- 'hn' | 'pdf'
    summary  TEXT,
    body     TEXT
);

-- Vector search (sqlite-vec)
CREATE VIRTUAL TABLE articles_vec USING vec0(
    article_id INTEGER PRIMARY KEY,
    embedding  FLOAT[1536]
);

-- Full-text search
CREATE VIRTUAL TABLE articles_fts USING fts5(
    title, summary, content=articles, content_rowid=id
);
```

**Config block** added to `~/.juggle/config.json`:

```json
"research_kb": {
  "db_path": "~/.juggle/research_kb.db",
  "embedding_model": "openai/text-embedding-3-small",
  "summarization_model": "google/gemini-3.1-flash",
  "hn_score_threshold": 100,
  "web_search_enabled": true,
  "pdf_dirs": []
}
```

All API calls (embeddings + summarization) route through `OPENROUTER_KEY` already in `~/.juggle/.env`. No additional API keys required.

---

## Section 2: Ingestion Pipeline

**Command:** `/juggle:research-ingest` (also called by `/juggle:init` on first setup)

**HN ingest:**
1. BigQuery export via `bq` CLI: query `bigquery-public-data.hacker_news.full` filtered by `score >= hn_score_threshold`, `type='story'`, `url IS NOT NULL`, and optional date range
2. Stream JSON output into `articles` table; skip existing URLs via `INSERT OR IGNORE`
3. Batch embed titles + summaries (100 articles/batch) via OpenRouter `openai/text-embedding-3-small`
4. Store vectors in `articles_vec`

**PDF ingest:**
1. For each directory in `config.research_kb.pdf_dirs`, scan for `*.pdf`
2. Extract text via `pypdf`, chunk into ~512-token segments
3. Embed each chunk; store as rows with `source='pdf'`, `url=file://...` (absolute path)
4. Skip already-ingested files (track by file path + mtime)

**Estimated one-time cost:** ~50k HN articles (5 years, score ≥ 100) → ~$0.50 in embeddings via OpenRouter.

**Incremental updates:** Re-run anytime; idempotent via `INSERT OR IGNORE`.

---

## Section 3: Research Command

**Files:**
- `/src/juggle_cmd_research.py` — standalone Python script, callable directly
- `/commands/research.md` — Juggle slash command spec
- `/.claude-plugin/plugin.json` — updated to register the command

**CLI interface:**

```
python juggle_cmd_research.py "topic" [--no-web] [--verbose]
```

**Flow:**

1. **Parallel search** across 4 sources:
   - **Local KB:** hybrid SQL using Reciprocal Rank Fusion (RRF) of vec0 KNN + FTS5 MATCH
   - **Vault:** grep-based search (reuses existing `cmd_grep_vault()` logic)
   - **Hindsight:** `recall` query (semantic, no reflection, no LLM cost)
   - **Web:** `mcp__web-search__search-web` (skipped if `--no-web` or `web_search_enabled: false`)

2. **Synthesize:** All results fed to `google/gemini-3.1-flash` via OpenRouter; produces a structured markdown digest.

3. **Output format (default):**
   ```
   ## [Topic]

   ### Articles
   - [Title](url) — one-line summary

   ### Books & Papers
   - [Title](url) — one-line summary

   ### From Your Notes
   - [Note title](obsidian://open?vault=personal&file=...) — excerpt

   ### Web
   - [Title](url) — one-line summary

   ### From Memory
   - Relevant Hindsight recall snippets (inline, no links)
   ```

4. **`--verbose` mode:** adds HN score, date, matched excerpt, and full Hindsight memory snippets per result.

5. All references use inline markdown links for clickability.

**Target latency:** <30s async total.

---

## Section 4: `/juggle:init` Integration

Two additions to `/commands/init.md`:

1. **New `research_kb` init step** (idempotent):
   - Creates `~/.juggle/research_kb.db` with schema if not present
   - Writes `research_kb` block to `~/.juggle/config.json` with defaults if not present
   - No new API key prompts — uses existing `OPENROUTER_KEY`
   - Prints: `"Run /juggle:research-ingest to populate the HN corpus"`

2. **Dependencies added to Juggle's Python environment:**
   - `sqlite-vec` (arm64 wheel ships on PyPI, works on Apple Silicon)
   - `pypdf` (PDF text extraction)
   - `httpx` (async OpenRouter calls)

Re-running init skips all steps where artifacts already exist.

---

## Out of Scope

- Real-time HN monitoring (ingest is manual/scheduled)
- Per-user multi-tenant support
- GUI or web UI
- Automatic PDF discovery outside configured `pdf_dirs`
