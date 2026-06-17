#!/usr/bin/env python3
"""Tests for juggle_cmd_research — search and synthesis."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


FAKE_EMBEDDING = [0.1] * 1536

FAKE_ARTICLE = {
    "id": 1,
    "title": "Async Python Guide",
    "url": "https://example.com/async",
    "score": 350,
    "date": "2024-03-01",
    "source": "hn",
    "summary": "A guide to asyncio",
    "rrf": 0.03,
}


@pytest.fixture
def kb(tmp_path):
    from juggle_research_kb import ResearchKB

    kb = ResearchKB(str(tmp_path / "test.db"))
    kb.init_db()
    kb.insert_article(
        title="Async Python Guide",
        url="https://example.com/async",
        score=350,
        date="2024-03-01",
        source="hn",
        summary="A guide to asyncio",
        body="Full asyncio guide content",
    )
    kb.upsert_embedding(1, FAKE_EMBEDDING)
    return kb


@pytest.mark.asyncio
async def test_search_kb_returns_results(kb):
    from juggle_cmd_research import search_kb

    with patch("juggle_cmd_research.get_query_embedding", return_value=FAKE_EMBEDDING):
        results = await search_kb(
            kb, "async python", api_key="key", model="openai/text-embedding-3-small"
        )
    assert len(results) >= 1
    assert results[0]["title"] == "Async Python Guide"


@pytest.mark.asyncio
async def test_search_vault_returns_paths(tmp_path):
    from juggle_cmd_research import search_vault

    md_file = tmp_path / "note.md"
    md_file.write_text("# Async Python\nThis is about asyncio")
    results = await search_vault("async", vault_path=str(tmp_path))
    assert str(md_file) in results


def test_synthesize_routes_through_llm_call():
    """synthesize() now delegates to the shared llm_call dispatcher (which owns
    the OpenRouter -> claude -p fallback) instead of calling OpenRouter directly."""
    from juggle_cmd_research import synthesize

    captured = {}

    def fake_llm_call(prompt, profile="cheap", timeout=10, max_tokens=None, json_mode=False):
        captured["profile"] = profile
        captured["max_tokens"] = max_tokens
        return "## Async Python\n\n- [Guide](https://example.com/async) — asyncio intro"

    with patch("llm_calls.llm_call", side_effect=fake_llm_call):
        result = synthesize(
            topic="async python",
            context="Articles: Async Python Guide https://example.com/async",
            vault_name="personal",
        )
    assert "Async Python" in result
    assert "https://example.com/async" in result
    assert captured["profile"] == "synthesis"
    assert captured["max_tokens"] >= 2048


def test_format_kb_results_default():
    from juggle_cmd_research import format_kb_results

    articles = [FAKE_ARTICLE]
    out = format_kb_results(articles, verbose=False)
    assert "Async Python Guide" in out
    assert "https://example.com/async" in out
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
