#!/usr/bin/env python3
"""Validate the design-system component-card library.

Walks design-system/components/<name>/index.html and asserts, for every card:

  1. line 1 is EXACTLY a ``<!-- @dsCard group="<Group>" -->`` marker,
  2. the HTML parses (html.parser raises on malformed markup), and
  3. every inline ``<style>`` block has balanced ``{ }`` braces
     (catches truncated / broken inline CSS).

It is the TDD gate for the card library: written BEFORE the cards exist (so it
goes RED on an empty tree — at least one card is required), then GREEN once the
cards are authored. Pure stdlib, no deps.

Usage:
    python3 scripts/check_design_cards.py [--json] [--root <dir>]

Exit code 0 = all cards valid, 1 = one or more failures (or zero cards found).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# Line 1 must be this and nothing else (group is a non-empty token).
_MARKER_RE = re.compile(r'^<!--\s*@dsCard\s+group="([^"]+)"\s*-->\s*$')


class _StrictParser(HTMLParser):
    """html.parser that records <style> bodies so we can brace-check them."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_style = False
        self.style_blocks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False

    def handle_data(self, data):
        if self._in_style:
            self.style_blocks.append(data)


def check_card(path: Path) -> list[str]:
    """Return a list of human-readable problems for one index.html (empty = OK)."""
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # (1) line-1 marker
    if not lines:
        return ["file is empty"]
    m = _MARKER_RE.match(lines[0])
    if not m:
        problems.append(f"line 1 is not a valid @dsCard marker: {lines[0]!r}")
    elif not m.group(1).strip():
        problems.append("@dsCard group is blank")

    # (2) parses + (3) collect style blocks
    parser = _StrictParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - report any parse failure
        problems.append(f"HTML failed to parse: {exc}")
        return problems

    # must actually have a <style> (these are self-contained styled previews)
    if not parser.style_blocks:
        problems.append("no <style> block found (cards must be self-contained)")

    # (3) brace balance per style block
    for i, block in enumerate(parser.style_blocks):
        if block.count("{") != block.count("}"):
            problems.append(
                f"<style> block #{i + 1} has unbalanced braces "
                f"({block.count('{')} '{{' vs {block.count('}')} '}}')"
            )

    return problems


def group_of(path: Path) -> str | None:
    first = path.read_text(encoding="utf-8").splitlines()[:1]
    if not first:
        return None
    m = _MARKER_RE.match(first[0])
    return m.group(1) if m else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=None, help="design-system dir (default: sibling of scripts/)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent / "design-system"
    comp_dir = root / "components"
    cards = sorted(comp_dir.glob("*/index.html"))

    results = []
    ok = 0
    for card in cards:
        problems = check_card(card)
        results.append(
            {
                "card": card.parent.name,
                "group": group_of(card),
                "ok": not problems,
                "problems": problems,
            }
        )
        if not problems:
            ok += 1

    failed = [r for r in results if not r["ok"]]
    # Zero cards is a FAILURE — keeps the gate RED before the library is authored.
    empty = len(cards) == 0
    groups = sorted({r["group"] for r in results if r["group"]})

    if args.json:
        print(json.dumps(
            {"total": len(cards), "ok": ok, "failed": len(failed),
             "groups": groups, "results": results}, indent=2))
    else:
        for r in results:
            mark = "OK " if r["ok"] else "FAIL"
            print(f"  [{mark}] {r['group'] or '?':<14} {r['card']}")
            for p in r["problems"]:
                print(f"         - {p}")
        if empty:
            print("FAIL: no cards found under design-system/components/*/index.html")
        print(
            f"\n{ok}/{len(cards)} cards valid across {len(groups)} groups "
            f"({', '.join(groups) or 'none'})"
        )

    return 0 if (ok == len(cards) and not empty) else 1


if __name__ == "__main__":
    raise SystemExit(main())
