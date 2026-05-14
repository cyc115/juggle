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
