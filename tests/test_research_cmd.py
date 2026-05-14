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
    assert "note" in out
