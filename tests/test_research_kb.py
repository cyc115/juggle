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
