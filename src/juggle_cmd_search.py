#!/usr/bin/env python3
"""juggle_cmd_search — raw search across KB and web, with optional Haiku filter pass.

Usage:
    python juggle_cmd_search.py "query" [--no-web] [--no-kb] [-k N]
    python juggle_cmd_search.py "query" --filter --web-results '[{...}]'
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

FILTER_PROMPT = """You are filtering and deduplicating search results for a user query.

Query: {query}

Raw results (KB articles + web snippets):
{raw}

Instructions:
- Remove duplicates (same topic/URL from multiple sources)
- Remove off-topic results
- Keep at most 5 KB results and 5 web results, the most relevant ones
- For each kept result, write a single crisp line: what makes it useful for this query
- Output valid JSON: {{"kb": [...], "web": [...]}}
- Each item: {{"title": "...", "url": "...", "reason": "one line why it's relevant"}}
- No prose outside the JSON
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


async def get_embedding(text: str, api_key: str, model: str) -> list[float]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": [text]},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


async def search_kb(query: str, api_key: str, model: str, k: int) -> list[dict]:
    sys.path.insert(0, str(Path(__file__).parent))
    from juggle_research_kb import ResearchKB
    from juggle_settings import get_settings

    s = get_settings()["research_kb"]
    db_path = str(Path(s["db_path"]).expanduser())
    kb = ResearchKB(db_path)
    embedding = await get_embedding(query, api_key, model)
    return kb.hybrid_search(embedding, query, k=k)


async def haiku_filter(query: str, kb_results: list[dict], web_results: list[dict], api_key: str) -> dict:
    raw = json.dumps({"kb": kb_results, "web": web_results}, indent=2)
    prompt = FILTER_PROMPT.format(query=query, raw=raw)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "anthropic/claude-haiku-4-5",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        # strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)


async def main(args: argparse.Namespace) -> None:
    _load_env()
    sys.path.insert(0, str(Path(__file__).parent))
    from juggle_settings import get_settings

    api_key = os.environ.get("OPENROUTER_KEY", "")
    model = get_settings()["research_kb"]["embedding_model"]

    kb_results: list[dict] = []
    web_results: list[dict] = []

    if not args.no_kb:
        if not api_key:
            print("Warning: OPENROUTER_KEY not set — skipping KB search", file=sys.stderr)
        else:
            kb_results = await search_kb(args.query, api_key, model, args.k)

    if args.web_results:
        web_results = json.loads(args.web_results)

    if args.filter and api_key and (kb_results or web_results):
        filtered = await haiku_filter(args.query, kb_results, web_results, api_key)
        print(json.dumps({"filtered": True, **filtered}, indent=2))
    else:
        out: dict = {}
        if kb_results:
            out["kb"] = kb_results
        if not args.no_web:
            out["web"] = "__use_mcp__"
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--no-kb", action="store_true")
    p.add_argument("--filter", action="store_true", help="Run Haiku filter pass on combined results")
    p.add_argument("--web-results", default="", help="JSON array of web results to include in filter")
    p.add_argument("-k", type=int, default=10)
    asyncio.run(main(p.parse_args()))
