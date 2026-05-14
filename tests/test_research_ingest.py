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
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"embedding": fake_embeddings[0], "index": 0},
                {"embedding": fake_embeddings[1], "index": 1},
            ]
        }
        mock_client.post = AsyncMock(return_value=mock_resp)

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
