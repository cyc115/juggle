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

Output format:
1. Start with a ## Summary section: 3-5 sentences synthesizing what you know about the topic based on the search results. Be direct, substantive, and analytical — not a list of sources.
2. Then source sections (omit if empty): ## Articles, ## Books & Papers, ## From Your Notes, ## Web, ## From Memory
3. Each source item format: `- Title — one-line summary\n  URL: <full url>`
4. Vault notes URL format: obsidian://open?vault=personal&file=<relative-path>
5. No inline markdown hyperlinks — always show the full URL on its own line prefixed with "URL: "
6. No filler, no preamble, no trailing paragraph
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


async def fetch_url_content(url: str, client: httpx.AsyncClient) -> str:
    try:
        resp = await client.get(
            url,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; research-bot)"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        if "text" not in resp.headers.get("content-type", ""):
            return ""
        return resp.text[:8000]
    except (httpx.HTTPError, OSError, UnicodeDecodeError):
        return ""


async def enrich_web_results(web_data: list[dict], deep: bool = False) -> list[dict]:
    if not web_data:
        return web_data
    batch_size = 5
    max_urls = min(15 if deep else 5, len(web_data))

    async with httpx.AsyncClient() as client:
        if not deep:
            batch = web_data[:max_urls]
            contents = await asyncio.gather(
                *[fetch_url_content(r["url"], client) for r in batch],
                return_exceptions=True,
            )
            for r, content in zip(batch, contents):
                if isinstance(content, str):
                    r["content"] = content
        else:
            prior_total = 0
            for batch_start in range(0, max_urls, batch_size):
                batch = web_data[batch_start : batch_start + batch_size]
                contents = await asyncio.gather(
                    *[fetch_url_content(r["url"], client) for r in batch],
                    return_exceptions=True,
                )
                batch_total = 0
                for r, content in zip(batch, contents):
                    if isinstance(content, str):
                        r["content"] = content
                        batch_total += len(content)
                if batch_start >= batch_size and prior_total > 0:
                    if batch_total < 0.2 * prior_total:
                        break
                prior_total += batch_total

    return web_data


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
        line = f"- {title}"
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
        line += f"\n  URL: {url}"
        lines.append(line)
    return "\n".join(lines)


def format_vault_results(paths: list[str], vault_path: str) -> str:
    lines = []
    vault_root = Path(vault_path)
    for p in paths:
        try:
            rel = Path(p).relative_to(vault_root)
        except ValueError:
            rel = Path(p)
        name = rel.stem
        encoded = str(rel).replace(" ", "%20")
        url = f"obsidian://open?vault=personal&file={encoded}"
        lines.append(f"- {name}\n  URL: {url}")
    return "\n".join(lines)


def format_web_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        title = r.get("title", r.get("url", "Link"))
        url = r.get("url", "")
        snippet = r.get("snippet", r.get("description", ""))
        content = r.get("content", "")
        line = f"- {title}"
        if snippet:
            line += f" — {snippet[:120]}"
        line += f"\n  URL: {url}"
        if content:
            # Include fetched page content, truncated, as additional context
            line += f"\n  Content: {content[:2000]}"
        lines.append(line)
    return "\n".join(lines)


async def run(topic: str, no_web: bool, verbose: bool, web_results_json: Optional[str], web_results_file: Optional[str] = None, deep: bool = False) -> None:
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
    if web_results_file:
        web_data = json.loads(Path(web_results_file).read_text())
    elif web_results_json:
        web_data = json.loads(web_results_json)
    else:
        web_data = []
    if isinstance(web_data, dict):
        web_data = web_data.get("results", [])

    if web_data and not no_web:
        web_data = await enrich_web_results(web_data, deep=deep)

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
    asyncio.run(run(
        topic=args.topic,
        no_web=getattr(args, "no_web", False),
        verbose=getattr(args, "verbose", False),
        web_results_json=getattr(args, "web_results", None),
        web_results_file=getattr(args, "web_results_file", None),
        deep=getattr(args, "deep", False),
    ))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Search research KB and synthesize results")
    p.add_argument("topic", help="Research topic")
    p.add_argument("--no-web", action="store_true", help="Skip web search results")
    p.add_argument("--verbose", action="store_true", help="Show raw results before synthesis")
    p.add_argument("--web-results", dest="web_results", default=None,
                   help="Web results as JSON string")
    p.add_argument("--web-results-file", dest="web_results_file", default=None,
                   help="Path to JSON file containing web results (preferred over --web-results)")
    p.add_argument("--deep", action="store_true", help="Incremental URL fetching up to 15 results")
    args = p.parse_args()
    asyncio.run(run(args.topic, args.no_web, args.verbose, args.web_results, args.web_results_file, args.deep))
