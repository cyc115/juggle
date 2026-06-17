#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["httpx", "sqlite-vec"]
# ///
"""Search the offline research KB (sqlite-vec + FTS5) only. No synthesis, no vault, no web."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path


def _load_env() -> None:
    env_path = Path.home() / ".juggle" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


async def _get_embedding(text: str, api_key: str, model: str) -> list[float]:
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://openrouter.ai/api/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "input": [text]},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def _fmt_results(results: list[dict], db_path: str, mode: str) -> str:
    lines = [f"{len(results)} result(s) — db={db_path}  mode={mode}", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        source = r.get("source", "")
        date = r.get("date", "")
        score = r.get("score", "")
        url = r.get("url", "")
        summary = (r.get("summary") or "")[:200]
        meta = "  ·  ".join(x for x in [source, date, f"score={score}" if score else ""] if x)
        lines.append(f"{i}. {title}")
        if meta:
            lines.append(f"   {meta}")
        if url:
            lines.append(f"   {url}")
        if summary:
            lines.append(f"   {summary}")
        lines.append("")
    return "\n".join(lines).rstrip()


async def main() -> None:
    p = argparse.ArgumentParser(description="Search offline research KB only (no synthesis)")
    p.add_argument("query", help="Search query")
    def _positive_int(val: str) -> int:
        n = int(val)
        if n < 1 or n > 100:
            raise argparse.ArgumentTypeError("limit must be between 1 and 100")
        return n

    p.add_argument("-k", "--limit", type=_positive_int, default=10, metavar="N", help="Max results 1-100 (default 10)")
    p.add_argument("--fts", action="store_true", help="FTS-only mode — fully offline, no embedding API call")
    p.add_argument("--json", dest="json_out", action="store_true", help="Emit raw JSON instead of pretty list")
    args = p.parse_args()

    _load_env()
    sys.path.insert(0, str(Path(__file__).parent))
    from juggle_research_kb import ResearchKB
    from juggle_settings import get_settings

    s = get_settings()["research_kb"]
    db_path = str(Path(s["db_path"]).expanduser())
    embedding_model = s["embedding_model"]

    kb = ResearchKB(db_path)
    kb.init_db()

    try:
        api_key = os.environ.get("OPENROUTER_KEY", "")
        if args.fts or not api_key:
            if not args.fts:
                print(
                    "Warning: OPENROUTER_KEY not set — semantic search unavailable -> "
                    "FTS keyword fallback",
                    file=sys.stderr,
                )
            results = kb.fts_search(args.query, limit=args.limit)
            mode = "fts"
        else:
            embedding = await _get_embedding(args.query, api_key, embedding_model)
            results = kb.hybrid_search(embedding, args.query, k=args.limit)
            mode = "hybrid"
    except sqlite3.OperationalError as e:
        print(f"DB error: {e}", file=sys.stderr)
        print("Run /juggle:research-ingest to initialize and populate the KB.", file=sys.stderr)
        sys.exit(1)

    if args.json_out:
        print(json.dumps(results, indent=2))
        return

    if not results:
        print(f"No results — db={db_path}  mode={mode}")
        print("Run /juggle:research-ingest to populate the KB first.")
        return

    print(_fmt_results(results, db_path, mode))


if __name__ == "__main__":
    asyncio.run(main())
