"""juggle_cockpit_tree — shared pure tree-layout helper for the cockpit Topics
pane (2026-06-30 topic-graph-state-unify R3). Renders a parent line plus either
indented child lines (expanded) or a single done/total rollup (collapsed). Pure
Rich; no DB, no Textual."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from rich.text import Text

_MERGED = frozenset({"verified", "done"})


@dataclass(frozen=True)
class TreeChild:
    id: str
    state: str


def tree_lines(
    parent_label: str,
    children: list[TreeChild],
    *,
    expanded: bool,
    glyph_for: Callable[[str], str],
    width: int,  # noqa: ARG001 — reserved for future truncation idiom
) -> list[Text]:
    out: list[Text] = [Text(parent_label, no_wrap=True, overflow="ellipsis")]
    if not children:
        return out
    if expanded:
        for c in children:
            out.append(
                Text(f"  └─ {glyph_for(c.state)} {c.id}",
                     no_wrap=True, overflow="ellipsis")
            )
    else:
        done = sum(1 for c in children if c.state in _MERGED)
        out.append(
            Text(f"  └─ {done}/{len(children)} done",
                 no_wrap=True, overflow="ellipsis")
        )
    return out
