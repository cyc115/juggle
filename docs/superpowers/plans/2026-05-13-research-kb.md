# Research KB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `/juggle:research [topic]` — a hybrid vector+keyword search over HN articles, PDFs, vault, and Hindsight, synthesized by Gemini 3.1 Flash via OpenRouter into a markdown digest with inline links.

**Architecture:** A dedicated SQLite DB (`~/.juggle/research_kb.db`) holds articles with sqlite-vec embeddings and FTS5 full-text index. The standalone `juggle_cmd_research.py` script runs parallel async searches across all sources, then calls OpenRouter for synthesis. The slash command handles MCP web search and injects results via `--web-results`.

**Tech Stack:** Python 3, sqlite-vec, FTS5, httpx (async), pypdf, OpenRouter API (embeddings + Gemini synthesis), BigQuery `bq` CLI (HN ingest)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/juggle_settings.py` | Modify | Add `research_kb` section to DEFAULTS |
| `src/juggle_research_kb.py` | Create | DB schema init, hybrid RRF search |
| `src/juggle_research_ingest.py` | Create | HN BigQuery + PDF ingestion pipeline |
| `src/juggle_cmd_research.py` | Create | Standalone search+synthesis CLI |
| `commands/research.md` | Create | `/juggle:research` slash command |
| `commands/research-ingest.md` | Create | `/juggle:research-ingest` slash command |
| `commands/init.md` | Modify | Add `research_kb` init step |
| `tests/test_research_kb.py` | Create | DB layer unit tests |
| `tests/test_research_ingest.py` | Create | Ingest pipeline tests (mocked) |
| `tests/test_research_cmd.py` | Create | Search/synthesis CLI tests (mocked) |

---

## Task 1: Add `research_kb` defaults to `juggle_settings.py`

**Files:**
- Modify: `src/juggle_settings.py`

- [ ] **Step 1: Add `research_kb` block to DEFAULTS dict**

In `src/juggle_settings.py`, add after the `"talkback"` block (before the closing `}`):

```python
    # Research Knowledge Base
    "research_kb": {
        "db_path": "~/.juggle/research_kb.db",
        "embedding_model": "openai/text-embedding-3-small",
        "summarization_model": "google/gemini-3.1-flash",
        "hn_score_threshold": 100,
        "web_search_enabled": True,
        "pdf_dirs": [],
    },
```

- [ ] **Step 2: Verify settings load correctly**

```bash
cd ~/github/juggle
python3 -c "
import sys; sys.path.insert(0, 'src')
from juggle_settings import get_settings
s = get_settings()
print(s['research_kb'])
"
```

Expected output:
```
{'db_path': '~/.juggle/research_kb.db', 'embedding_model': 'openai/text-embedding-3-small', 'summarization_model': 'google/gemini-3.1-flash', 'hn_score_threshold': 100, 'web_search_enabled': True, 'pdf_dirs': []}
```

- [ ] **Step 3: Commit**

```bash
cd ~/github/juggle
git add src/juggle_settings.py
git commit -m "feat(research-kb): add research_kb defaults to settings"
```

---

## Task 2: Create `src/juggle_research_kb.py` — DB layer

**Files:**
- Create: `src/juggle_research_kb.py`
- Create: `tests/test_research_kb.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_kb.py`:

```python
#!/usr/bin/env python3
"""Tests for juggle_research_kb — DB init and hybrid search."""
import sys
import sqlite3
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_research_kb.db")


@pytest.fixture
def kb(db_path):
    from juggle_research_kb import ResearchKB
    kb = ResearchKB(db_path)
    kb.init_db()
    return kb


def test_init_db_creates_tables(db_path):
    from juggle_research_kb import ResearchKB
    kb = ResearchKB(db_path)
    kb.init_db()
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' OR type='shadow'").fetchall()}
    assert "articles" in tables
    conn.close()


def test_insert_article(kb):
    kb.insert_article(
        title="Test Article",
        url="https://example.com/test",
        score=200,
        date="2024-01-01",
        source="hn",
        summary="A test article",
        body="Full body text here",
    )
    conn = sqlite3.connect(kb.db_path)
    row = conn.execute("SELECT title, score FROM articles WHERE url=?", ("https://example.com/test",)).fetchone()
    conn.close()
    assert row == ("Test Article", 200)


def test_insert_article_idempotent(kb):
    for _ in range(3):
        kb.insert_article(
            title="Dupe", url="https://example.com/dupe",
            score=100, date="2024-01-01", source="hn",
            summary="s", body="b",
        )
    conn = sqlite3.connect(kb.db_path)
    count = conn.execute("SELECT count(*) FROM articles WHERE url=?", ("https://example.com/dupe",)).fetchone()[0]
    conn.close()
    assert count == 1


def test_upsert_embedding(kb):
    kb.insert_article(
        title="Vec Article", url="https://example.com/vec",
        score=150, date="2024-01-01", source="hn",
        summary="s", body="b",
    )
    conn = sqlite3.connect(kb.db_path)
    article_id = conn.execute("SELECT id FROM articles WHERE url=?", ("https://example.com/vec",)).fetchone()[0]
    conn.close()
    embedding = [0.1] * 1536
    kb.upsert_embedding(article_id, embedding)


def test_fts_search_returns_results(kb):
    kb.insert_article(
        title="Python async programming", url="https://example.com/py",
        score=300, date="2024-01-01", source="hn",
        summary="Learn async in Python", body="asyncio guide",
    )
    results = kb.fts_search("async python", limit=5)
    assert len(results) >= 1
    assert results[0]["title"] == "Python async programming"


def test_pdf_file_tracking(kb, tmp_path):
    pdf_path = str(tmp_path / "test.pdf")
    Path(pdf_path).write_bytes(b"fake")
    assert not kb.is_pdf_ingested(pdf_path, mtime=1.0)
    kb.mark_pdf_ingested(pdf_path, mtime=1.0)
    assert kb.is_pdf_ingested(pdf_path, mtime=1.0)
    assert not kb.is_pdf_ingested(pdf_path, mtime=2.0)  # mtime changed
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_kb.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'juggle_research_kb'`

- [ ] **Step 3: Install sqlite-vec**

```bash
pip install sqlite-vec
python3 -c "import sqlite_vec; print('sqlite-vec ok')"
```

- [ ] **Step 4: Create `src/juggle_research_kb.py`**

```python
#!/usr/bin/env python3
"""Research KB — SQLite DB layer with sqlite-vec + FTS5 hybrid search."""
import sqlite3
import struct
from pathlib import Path
from typing import Optional


def _serialize_f32(values: list[float]) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


class ResearchKB:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).expanduser())

    def _connect(self) -> sqlite3.Connection:
        import sqlite_vec
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        sqlite_vec.load(conn)
        return conn

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id      INTEGER PRIMARY KEY,
                title   TEXT NOT NULL,
                url     TEXT UNIQUE NOT NULL,
                score   INTEGER,
                date    TEXT,
                source  TEXT NOT NULL,
                summary TEXT,
                body    TEXT
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS articles_vec USING vec0(
                article_id INTEGER PRIMARY KEY,
                embedding  FLOAT[1536]
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                title, summary,
                content=articles, content_rowid=id
            );

            CREATE TABLE IF NOT EXISTS pdf_files (
                path         TEXT PRIMARY KEY,
                mtime        REAL NOT NULL,
                ingested_at  TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

    def insert_article(
        self, title: str, url: str, score: Optional[int], date: Optional[str],
        source: str, summary: Optional[str], body: Optional[str],
    ) -> Optional[int]:
        """Insert article; skip if URL exists. Returns new row id or None."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO articles (title, url, score, date, source, summary, body)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (title, url, score, date, source, summary, body),
            )
            conn.commit()
            return cur.lastrowid if cur.rowcount else None
        finally:
            conn.close()

    def upsert_embedding(self, article_id: int, embedding: list[float]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO articles_vec (article_id, embedding) VALUES (?, ?)",
                (article_id, _serialize_f32(embedding)),
            )
            conn.commit()
        finally:
            conn.close()

    def get_article_id(self, url: str) -> Optional[int]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT id FROM articles WHERE url=?", (url,)).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT a.id, a.title, a.url, a.score, a.date, a.source, a.summary,
                          row_number() OVER (ORDER BY f.rank) AS rnk
                   FROM articles_fts f
                   JOIN articles a ON a.id = f.rowid
                   WHERE articles_fts MATCH ?
                   LIMIT ?""",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def hybrid_search(self, query_embedding: list[float], query_text: str, k: int = 10) -> list[dict]:
        """RRF fusion of vec0 KNN and FTS5. Returns top-k results."""
        conn = self._connect()
        try:
            blob = _serialize_f32(query_embedding)
            rows = conn.execute(
                """
                WITH vec_hits AS (
                    SELECT article_id AS id,
                           row_number() OVER (ORDER BY distance) AS rnk
                    FROM articles_vec
                    WHERE embedding MATCH ? AND k = 20
                ),
                fts_hits AS (
                    SELECT rowid AS id,
                           row_number() OVER (ORDER BY rank) AS rnk
                    FROM articles_fts
                    WHERE articles_fts MATCH ?
                    LIMIT 20
                ),
                uniq AS (
                    SELECT id FROM vec_hits
                    UNION
                    SELECT id FROM fts_hits
                )
                SELECT a.id, a.title, a.url, a.score, a.date, a.source, a.summary,
                       (1.0/(60 + COALESCE(v.rnk, 60)) + 1.0/(60 + COALESCE(f.rnk, 60))) AS rrf
                FROM uniq
                JOIN articles a ON a.id = uniq.id
                LEFT JOIN vec_hits v ON v.id = uniq.id
                LEFT JOIN fts_hits f ON f.id = uniq.id
                ORDER BY rrf DESC
                LIMIT ?
                """,
                [blob, query_text, k],
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def is_pdf_ingested(self, path: str, mtime: float) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT mtime FROM pdf_files WHERE path=?", (path,)
            ).fetchone()
            return row is not None and abs(row["mtime"] - mtime) < 0.01
        finally:
            conn.close()

    def mark_pdf_ingested(self, path: str, mtime: float) -> None:
        from datetime import datetime, timezone
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO pdf_files (path, mtime, ingested_at) VALUES (?, ?, ?)",
                (path, mtime, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_kb.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_research_kb.py tests/test_research_kb.py
git commit -m "feat(research-kb): add DB layer with sqlite-vec + FTS5 hybrid search"
```

---

## Task 3: Create `src/juggle_research_ingest.py` — ingestion pipeline

**Files:**
- Create: `src/juggle_research_ingest.py`
- Create: `tests/test_research_ingest.py`

- [ ] **Step 1: Install dependencies**

```bash
pip install pypdf httpx pytest-asyncio
python3 -c "import pypdf, httpx; print('deps ok')"
```

- [ ] **Step 2: Write failing tests**

Create `tests/test_research_ingest.py`:

```python
#!/usr/bin/env python3
"""Tests for juggle_research_ingest — HN and PDF ingestion."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def kb(tmp_path):
    from juggle_research_kb import ResearchKB
    kb = ResearchKB(str(tmp_path / "test.db"))
    kb.init_db()
    return kb


def test_parse_bq_row():
    from juggle_research_ingest import parse_bq_row
    row = {
        "title": "Ask HN: Best books 2024",
        "url": "https://news.ycombinator.com/item?id=123",
        "score": 250,
        "time": 1704067200,
        "text": None,
    }
    article = parse_bq_row(row)
    assert article["title"] == "Ask HN: Best books 2024"
    assert article["score"] == 250
    assert article["date"] == "2024-01-01"
    assert article["source"] == "hn"


def test_parse_bq_row_skips_missing_url():
    from juggle_research_ingest import parse_bq_row
    assert parse_bq_row({"title": "x", "url": None, "score": 100, "time": 0, "text": None}) is None


def test_chunk_text():
    from juggle_research_ingest import chunk_text
    text = "word " * 600
    chunks = chunk_text(text, max_tokens=512)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk.split()) <= 520  # slight tolerance


@pytest.mark.asyncio
async def test_embed_batch_calls_openrouter(kb):
    from juggle_research_ingest import embed_batch
    fake_embeddings = [[0.1] * 1536, [0.2] * 1536]
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [{"embedding": e} for e in fake_embeddings]
        }
        mock_post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await embed_batch(
            texts=["hello", "world"],
            model="openai/text-embedding-3-small",
            api_key="test-key",
        )
    assert len(result) == 2
    assert len(result[0]) == 1536


def test_ingest_hn_rows(kb):
    from juggle_research_ingest import ingest_hn_rows
    rows = [
        {"title": "Rust is great", "url": "https://example.com/rust", "score": 300,
         "time": 1704067200, "text": "Rust memory safety"},
        {"title": "No URL", "url": None, "score": 100, "time": 1704067200, "text": None},
    ]
    count = ingest_hn_rows(kb, rows)
    assert count == 1


def test_ingest_pdf_skips_already_ingested(kb, tmp_path):
    from juggle_research_ingest import should_ingest_pdf
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"fake pdf content")
    mtime = pdf_path.stat().st_mtime

    assert should_ingest_pdf(kb, str(pdf_path)) is True
    kb.mark_pdf_ingested(str(pdf_path), mtime)
    assert should_ingest_pdf(kb, str(pdf_path)) is False
```

- [ ] **Step 3: Run tests — verify they fail**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_ingest.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'juggle_research_ingest'`

- [ ] **Step 4: Create `src/juggle_research_ingest.py`**

```python
#!/usr/bin/env python3
"""Research KB ingestion — HN via BigQuery bq CLI, PDFs via pypdf."""
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx


def _load_env() -> None:
    env_path = Path.home() / ".juggle" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def parse_bq_row(row: dict) -> Optional[dict]:
    if not row.get("url"):
        return None
    ts = row.get("time", 0)
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else None
    return {
        "title": row.get("title", ""),
        "url": row["url"],
        "score": row.get("score"),
        "date": date,
        "source": "hn",
        "summary": (row.get("text") or "")[:500] or None,
        "body": row.get("text"),
    }


def chunk_text(text: str, max_tokens: int = 512) -> list[str]:
    words = text.split()
    chunks, current = [], []
    for word in words:
        current.append(word)
        if len(current) >= max_tokens:
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


async def embed_batch(texts: list[str], model: str, api_key: str) -> list[list[float]]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": texts},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        data.sort(key=lambda x: x["index"])
        return [d["embedding"] for d in data]


def ingest_hn_rows(kb, rows: list[dict]) -> int:
    count = 0
    for row in rows:
        article = parse_bq_row(row)
        if article:
            row_id = kb.insert_article(**article)
            if row_id:
                count += 1
    return count


async def embed_pending(kb, model: str, api_key: str, batch_size: int = 100) -> int:
    """Embed articles that have no vector yet. Returns count embedded."""
    import sqlite3
    conn = sqlite3.connect(kb.db_path)
    rows = conn.execute(
        """SELECT a.id, a.title, a.summary FROM articles a
           WHERE NOT EXISTS (SELECT 1 FROM articles_vec v WHERE v.article_id = a.id)"""
    ).fetchall()
    conn.close()

    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts = [f"{r[1]}. {r[2] or ''}" for r in batch]
        embeddings = await embed_batch(texts, model, api_key)
        for (article_id, _, _), emb in zip(batch, embeddings):
            kb.upsert_embedding(article_id, emb)
        total += len(batch)
        print(f"  Embedded {total}/{len(rows)}", flush=True)
    return total


def should_ingest_pdf(kb, path: str) -> bool:
    mtime = Path(path).stat().st_mtime
    return not kb.is_pdf_ingested(path, mtime)


async def ingest_pdf(kb, pdf_path: str, model: str, api_key: str) -> int:
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    full_text = "\n".join(
        page.extract_text() or "" for page in reader.pages
    )
    chunks = chunk_text(full_text, max_tokens=512)
    if not chunks:
        return 0

    filename = Path(pdf_path).stem
    embeddings = await embed_batch(chunks, model, api_key)

    count = 0
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        url = f"file://{pdf_path}#chunk{i}"
        row_id = kb.insert_article(
            title=f"{filename} (chunk {i+1}/{len(chunks)})",
            url=url,
            score=None,
            date=None,
            source="pdf",
            summary=chunk[:200],
            body=chunk,
        )
        if row_id:
            kb.upsert_embedding(row_id, emb)
            count += 1

    mtime = Path(pdf_path).stat().st_mtime
    kb.mark_pdf_ingested(pdf_path, mtime)
    return count


async def run_hn_ingest(kb, score_threshold: int, model: str, api_key: str,
                        years_back: int = 5) -> None:
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    query = (
        f"SELECT title, url, score, time, text "
        f"FROM `bigquery-public-data.hacker_news.full` "
        f"WHERE type='story' AND url IS NOT NULL AND score >= {score_threshold} "
        f"AND TIMESTAMP_SECONDS(time) >= '{cutoff}' "
        f"LIMIT 100000"
    )
    print(f"Running BigQuery export (score>={score_threshold}, since {cutoff})...")
    result = subprocess.run(
        ["bq", "query", "--format=newline_delimited_json", "--nouse_legacy_sql", query],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        print(f"BigQuery error: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    inserted = ingest_hn_rows(kb, rows)
    print(f"Inserted {inserted} new articles from {len(rows)} rows")
    embedded = await embed_pending(kb, model, api_key)
    print(f"Embedded {embedded} articles")


async def run_pdf_ingest(kb, pdf_dirs: list[str], model: str, api_key: str) -> None:
    for dir_path in pdf_dirs:
        d = Path(dir_path).expanduser()
        if not d.exists():
            print(f"PDF dir not found, skipping: {d}")
            continue
        for pdf in d.glob("*.pdf"):
            if should_ingest_pdf(kb, str(pdf)):
                print(f"Ingesting {pdf.name}...")
                count = await ingest_pdf(kb, str(pdf), model, api_key)
                print(f"  -> {count} chunks")
            else:
                print(f"  Skipping (unchanged): {pdf.name}")


async def main(args) -> None:
    _load_env()
    sys.path.insert(0, str(Path(__file__).parent))
    from juggle_research_kb import ResearchKB
    from juggle_settings import get_settings

    s = get_settings()["research_kb"]
    db_path = str(Path(s["db_path"]).expanduser())
    model = s["embedding_model"]
    api_key = os.environ.get("OPENROUTER_KEY", "")
    if not api_key:
        print("Error: OPENROUTER_KEY not set in ~/.juggle/.env", file=sys.stderr)
        sys.exit(1)

    kb = ResearchKB(db_path)
    kb.init_db()

    if not args.pdf_only:
        await run_hn_ingest(kb, s["hn_score_threshold"], model, api_key)

    if s["pdf_dirs"] or args.pdf_only:
        await run_pdf_ingest(kb, s["pdf_dirs"], model, api_key)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Ingest HN articles and PDFs into research KB")
    p.add_argument("--pdf-only", action="store_true", help="Skip HN ingest, only process PDFs")
    asyncio.run(main(p.parse_args()))
```

- [ ] **Step 5: Run tests — verify they pass**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_ingest.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_research_ingest.py tests/test_research_ingest.py
git commit -m "feat(research-kb): add HN + PDF ingestion pipeline"
```

---

## Task 4: Create `src/juggle_cmd_research.py` — search + synthesis CLI

**Files:**
- Create: `src/juggle_cmd_research.py`
- Create: `tests/test_research_cmd.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_research_cmd.py`:

```python
#!/usr/bin/env python3
"""Tests for juggle_cmd_research — search and synthesis."""
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


FAKE_EMBEDDING = [0.1] * 1536

FAKE_ARTICLE = {
    "id": 1, "title": "Async Python Guide", "url": "https://example.com/async",
    "score": 350, "date": "2024-03-01", "source": "hn",
    "summary": "A guide to asyncio", "rrf": 0.03,
}


@pytest.fixture
def kb(tmp_path):
    from juggle_research_kb import ResearchKB
    kb = ResearchKB(str(tmp_path / "test.db"))
    kb.init_db()
    kb.insert_article(
        title="Async Python Guide", url="https://example.com/async",
        score=350, date="2024-03-01", source="hn",
        summary="A guide to asyncio", body="Full asyncio guide content",
    )
    kb.upsert_embedding(1, FAKE_EMBEDDING)
    return kb


@pytest.mark.asyncio
async def test_search_kb_returns_results(kb):
    from juggle_cmd_research import search_kb
    with patch("juggle_cmd_research.get_query_embedding", return_value=FAKE_EMBEDDING):
        results = await search_kb(kb, "async python", api_key="key", model="openai/text-embedding-3-small")
    assert len(results) >= 1
    assert results[0]["title"] == "Async Python Guide"


@pytest.mark.asyncio
async def test_search_vault_returns_paths(tmp_path):
    from juggle_cmd_research import search_vault
    md_file = tmp_path / "note.md"
    md_file.write_text("# Async Python\nThis is about asyncio")
    results = await search_vault("async", vault_path=str(tmp_path))
    assert str(md_file) in results


@pytest.mark.asyncio
async def test_synthesize_calls_openrouter():
    from juggle_cmd_research import synthesize
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "## Async Python\n\n- [Guide](https://example.com/async) — asyncio intro"}}]
        }
        mock_client.post = AsyncMock(return_value=mock_resp)
        result = await synthesize(
            topic="async python",
            context="Articles: Async Python Guide https://example.com/async",
            model="google/gemini-3.1-flash",
            api_key="test-key",
        )
    assert "Async Python" in result
    assert "https://example.com/async" in result


def test_format_kb_results_default():
    from juggle_cmd_research import format_kb_results
    articles = [FAKE_ARTICLE]
    out = format_kb_results(articles, verbose=False)
    assert "[Async Python Guide](https://example.com/async)" in out
    assert "score" not in out.lower()


def test_format_kb_results_verbose():
    from juggle_cmd_research import format_kb_results
    articles = [FAKE_ARTICLE]
    out = format_kb_results(articles, verbose=True)
    assert "350" in out  # score shown in verbose
    assert "2024-03-01" in out


def test_format_vault_results():
    from juggle_cmd_research import format_vault_results
    paths = ["/Users/mike/Documents/personal/knowledge/note.md"]
    out = format_vault_results(paths, vault_path="/Users/mike/Documents/personal")
    assert "obsidian://" in out
    assert "[note]" in out or "note" in out
```

- [ ] **Step 2: Run tests — verify they fail**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_cmd.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'juggle_cmd_research'`

- [ ] **Step 3: Create `src/juggle_cmd_research.py`**

```python
#!/usr/bin/env python3
"""juggle_cmd_research — standalone research KB search + synthesis CLI.

Usage (standalone):
    python juggle_cmd_research.py "topic" [--no-web] [--verbose]
    python juggle_cmd_research.py "topic" --web-results '{"results":[...]}'

Usage (from Juggle CLI):
    juggle_cli.py research "topic" [--no-web] [--verbose] [--web-results JSON]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx

SYNTHESIS_PROMPT = """You are a research assistant synthesizing results for a personal knowledge base.

Topic: {topic}

Search results:
{context}

Rules:
- Use ONLY inline markdown links [Title](url) — never bare URLs
- Vault notes: use obsidian://open?vault=personal&file=<relative-path> links
- Sections (omit if empty): ## Articles, ## Books & Papers, ## From Your Notes, ## Web, ## From Memory
- Each item: `- [Title](url) — one-line summary`
- No filler, no preamble, no trailing paragraph
"""


def _load_env() -> None:
    env_path = Path.home() / ".juggle" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


async def get_query_embedding(text: str, api_key: str, model: str) -> list[float]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": [text]},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def search_kb(kb, query: str, api_key: str, model: str, k: int = 10) -> list[dict]:
    embedding = await get_query_embedding(query, api_key, model)
    return kb.hybrid_search(embedding, query, k=k)


async def search_vault(query: str, vault_path: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["grep", "-ril", "--include=*.md", query, vault_path],
            capture_output=True, text=True, timeout=5,
        )
        return [p for p in proc.stdout.strip().split("\n") if p][:20]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


async def search_hindsight(query: str) -> str:
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from juggle_hindsight import HindsightClient
        client = HindsightClient.from_config()
        if client is None:
            return ""
        result = client.recall(query)
        return result or ""
    except Exception:
        return ""


async def synthesize(topic: str, context: str, model: str, api_key: str) -> str:
    prompt = SYNTHESIS_PROMPT.format(topic=topic, context=context)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}]},
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def format_kb_results(articles: list[dict], verbose: bool) -> str:
    lines = []
    for a in articles:
        url = a["url"]
        title = a["title"]
        summary = a.get("summary") or ""
        line = f"- [{title}]({url})"
        if summary:
            line += f" — {summary[:120]}"
        if verbose:
            meta = []
            if a.get("score"):
                meta.append(f"score={a['score']}")
            if a.get("date"):
                meta.append(a["date"])
            if meta:
                line += f" ({', '.join(meta)})"
        lines.append(line)
    return "\n".join(lines)


def format_vault_results(paths: list[str], vault_path: str) -> str:
    lines = []
    vault_root = Path(vault_path)
    for p in paths:
        rel = Path(p).relative_to(vault_root) if vault_path and Path(p).is_relative_to(vault_root) else Path(p)
        name = rel.stem
        encoded = str(rel).replace(" ", "%20")
        url = f"obsidian://open?vault=personal&file={encoded}"
        lines.append(f"- [{name}]({url})")
    return "\n".join(lines)


def format_web_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        title = r.get("title", r.get("url", "Link"))
        url = r.get("url", "")
        snippet = r.get("snippet", r.get("description", ""))
        line = f"- [{title}]({url})"
        if snippet:
            line += f" — {snippet[:120]}"
        lines.append(line)
    return "\n".join(lines)


async def run(topic: str, no_web: bool, verbose: bool, web_results_json: Optional[str]) -> None:
    _load_env()
    sys.path.insert(0, str(Path(__file__).parent))
    from juggle_research_kb import ResearchKB
    from juggle_settings import get_settings

    s = get_settings()["research_kb"]
    api_key = os.environ.get("OPENROUTER_KEY", "")
    if not api_key:
        print("Error: OPENROUTER_KEY not set in ~/.juggle/.env", file=sys.stderr)
        sys.exit(1)

    vault_path = str(Path("~/Documents/personal").expanduser())
    db_path = str(Path(s["db_path"]).expanduser())
    embedding_model = s["embedding_model"]
    synthesis_model = s["summarization_model"]
    use_web = not no_web and s.get("web_search_enabled", True)

    kb = ResearchKB(db_path)

    # Parallel search
    tasks = {
        "kb": search_kb(kb, topic, api_key, embedding_model),
        "vault": search_vault(topic, vault_path),
        "hindsight": search_hindsight(topic),
    }
    results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values(), return_exceptions=True)))

    kb_articles = results["kb"] if isinstance(results["kb"], list) else []
    vault_paths = results["vault"] if isinstance(results["vault"], list) else []
    memory_text = results["hindsight"] if isinstance(results["hindsight"], str) else ""
    web_data = json.loads(web_results_json) if web_results_json else []
    if isinstance(web_data, dict):
        web_data = web_data.get("results", [])

    if verbose:
        print(f"\n=== KB results ({len(kb_articles)}) ===")
        print(format_kb_results(kb_articles, verbose=True))
        print(f"\n=== Vault results ({len(vault_paths)}) ===")
        print(format_vault_results(vault_paths, vault_path))
        if memory_text:
            print(f"\n=== Memory ===\n{memory_text}")
        if web_data:
            print(f"\n=== Web results ({len(web_data)}) ===")
            print(format_web_results(web_data))
        print("\n=== Synthesis ===")

    # Build context for synthesis
    context_parts = []
    if kb_articles:
        context_parts.append("## Articles from KB\n" + format_kb_results(kb_articles, verbose=False))
    if vault_paths:
        context_parts.append("## Vault Notes\n" + format_vault_results(vault_paths, vault_path))
    if memory_text:
        context_parts.append(f"## From Memory\n{memory_text}")
    if web_data:
        context_parts.append("## Web Results\n" + format_web_results(web_data))

    if not context_parts:
        print(f"No results found for: {topic}")
        return

    digest = await synthesize(topic, "\n\n".join(context_parts), synthesis_model, api_key)
    print(digest)


def cmd_research(args) -> None:
    """Entry point called by juggle_cli.py."""
    web_results_json = getattr(args, "web_results", None)
    asyncio.run(run(
        topic=args.topic,
        no_web=getattr(args, "no_web", False),
        verbose=getattr(args, "verbose", False),
        web_results_json=web_results_json,
    ))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Search research KB and synthesize results")
    p.add_argument("topic", help="Research topic")
    p.add_argument("--no-web", action="store_true", help="Skip web search results")
    p.add_argument("--verbose", action="store_true", help="Show raw results before synthesis")
    p.add_argument("--web-results", dest="web_results", default=None,
                   help="Pre-fetched web results as JSON string")
    args = p.parse_args()
    asyncio.run(run(args.topic, args.no_web, args.verbose, args.web_results))
```

- [ ] **Step 4: Run tests — verify they pass**

```bash
cd ~/github/juggle
python3 -m pytest tests/test_research_cmd.py -v
```

Expected: all 6 tests PASS.

- [ ] **Step 5: Smoke test (requires populated DB)**

Skip if KB not yet populated. If populated:

```bash
cd ~/github/juggle
source ~/.juggle/.env
python3 src/juggle_cmd_research.py "rust programming" --verbose
```

Expected: markdown digest with inline links printed to stdout.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_research.py tests/test_research_cmd.py
git commit -m "feat(research-kb): add standalone research search + synthesis CLI"
```

---

## Task 5: Register `research` and `research-ingest` commands in Juggle CLI

**Files:**
- Modify: `src/juggle_cli.py`
- Create: `commands/research.md`
- Create: `commands/research-ingest.md`

- [ ] **Step 1: Add `research` subparser to `juggle_cli.py`**

In `src/juggle_cli.py`, find where context commands are imported and add:

```python
from juggle_cmd_research import cmd_research
```

Then in the argparse section (where other subparsers are registered), add:

```python
    # research
    p_research = subparsers.add_parser("research", help="Search research KB")
    p_research.add_argument("topic", help="Research topic")
    p_research.add_argument("--no-web", action="store_true")
    p_research.add_argument("--verbose", action="store_true")
    p_research.add_argument("--web-results", dest="web_results", default=None)
    p_research.set_defaults(func=cmd_research)
```

- [ ] **Step 2: Verify CLI registration**

```bash
cd ~/github/juggle
python3 src/juggle_cli.py research --help
```

Expected output includes: `usage: juggle_cli.py research [-h] [--no-web] [--verbose] [--web-results WEB_RESULTS] topic`

- [ ] **Step 3: Create `commands/research.md`**

```markdown
---
description: Search research KB — HN articles, PDFs, vault, memory, and web
allowed-tools: Bash, mcp__web-search__search-web
---

# /juggle:research — Research Knowledge Base

Search for a topic across HN articles, PDFs, vault notes, Hindsight memory, and the web.

**Usage:** `/juggle:research <topic>`

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

### 2. If web search enabled, run web search

Use `mcp__web-search__search-web` with the topic as query. Collect results into a JSON array:
```json
[{"title": "...", "url": "...", "snippet": "..."}]
```

Store as `WEB_JSON` variable (single-quoted JSON string). If web search disabled or `--no-web` passed, set `WEB_JSON=""`.

### 3. Run research command

```bash
source ~/.juggle/.env 2>/dev/null; true
python3 ${CLAUDE_PLUGIN_ROOT}/src/juggle_cmd_research.py "<TOPIC>" \
  ${VERBOSE_FLAG} \
  ${WEB_JSON:+--web-results "$WEB_JSON"}
```

- Set `VERBOSE_FLAG=--verbose` if user passed `--verbose`, otherwise empty string.
- The script prints the markdown digest to stdout. Print it to the user.

### 4. If DB not found

If the script exits with error about missing DB, tell the user:
> "Research KB not initialized. Run `/juggle:init` first, then `/juggle:research-ingest` to populate the HN corpus."
```

- [ ] **Step 4: Create `commands/research-ingest.md`**

```markdown
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
```

- [ ] **Step 5: Run existing tests to confirm no regressions**

```bash
cd ~/github/juggle
python3 -m pytest tests/ -v --ignore=tests/test_research_kb.py --ignore=tests/test_research_ingest.py --ignore=tests/test_research_cmd.py -x -q 2>&1 | tail -20
```

Expected: all existing tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cli.py commands/research.md commands/research-ingest.md
git commit -m "feat(research-kb): add research slash commands and CLI registration"
```

---

## Task 6: Update `/juggle:init` and `plugin.json`

**Files:**
- Modify: `commands/init.md`
- Modify: `.claude-plugin/plugin.json`

- [ ] **Step 1: Add `research_kb` step to `commands/init.md`**

At the end of `commands/init.md`, before the final confirmation message, add a new step:

```markdown
### 5. Initialize Research Knowledge Base

```bash
python3 -c "
import sys, json
sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_research_kb import ResearchKB
from juggle_settings import get_settings
import os
from pathlib import Path

s = get_settings()['research_kb']
db_path = str(Path(s['db_path']).expanduser())
kb = ResearchKB(db_path)
kb.init_db()
print(f'Research KB initialized at {db_path}')
"
```

Then update `~/.juggle/config.json` to include the `research_kb` block if missing:

```bash
python3 - << 'PYEOF'
import sys, json, copy
from pathlib import Path

sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/src')
from juggle_settings import DEFAULTS

config_path = Path.home() / ".juggle" / "config.json"
current = json.loads(config_path.read_text()) if config_path.exists() else {}

if "research_kb" not in current:
    current["research_kb"] = copy.deepcopy(DEFAULTS["research_kb"])
    config_path.write_text(json.dumps(current, indent=2))
    print("Added research_kb config block")
else:
    print("research_kb config already present — skipped")
PYEOF
```

Tell the user: "Research KB ready. Run `/juggle:research-ingest` to populate the HN corpus (~5 min, ~$0.50 in embeddings)."
```

- [ ] **Step 2: Bump version in `plugin.json`**

In `.claude-plugin/plugin.json`, increment the patch version (e.g. `1.19.3` → `1.19.4`) and add `"research"` to the keywords array:

```json
{
  "name": "juggle",
  "description": "Multi-topic conversation orchestrator. Manage parallel conversation threads within a single Claude Code session — discuss one topic while background agents research or build for others.",
  "version": "1.19.4",
  "author": {
    "name": "Mike Chen"
  },
  "keywords": [
    "productivity",
    "conversations",
    "topics",
    "multi-threading",
    "background-agents",
    "research"
  ]
}
```

- [ ] **Step 3: Run full test suite**

```bash
cd ~/github/juggle
python3 -m pytest tests/ -v -q 2>&1 | tail -20
```

Expected: all tests pass.

- [ ] **Step 4: Final commit**

```bash
cd ~/github/juggle
git add commands/init.md .claude-plugin/plugin.json
git commit -m "feat(research-kb): wire research_kb into /juggle:init, bump plugin to v1.19.4"
```
