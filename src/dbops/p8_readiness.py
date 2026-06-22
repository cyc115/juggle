"""P8 readiness harness — the two programmatic gates that make the legacy-table
drop safe. Pure functions, agent-verifiable via `juggle doctor --pre-p8-check`.

Gate A (static):  scan_legacy_refs() — zero steady-state source refs remain.
Gate B (runtime): p8_drop_ready() — nodes fully mirrors legacy (lossless drop).

No side effects: every function here is read-only over its inputs.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_LEGACY = ("threads", "graph_topics", "graph_tasks", "graph_edges")


# ── Gate B: runtime data-readiness predicate ──────────────────────────────────

def p8_drop_ready(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """True iff the legacy tables are safe to drop: every legacy row is mirrored
    into nodes/node_edges (id-anchored anti-join == 0) AND integrity holds (no
    NULL nodes.title, every parent_id resolvable). Returns (False,
    ["already-dropped"]) when the legacy tables are already gone (idempotent
    re-run)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    present = set(_LEGACY) & tables
    if not present:
        return False, ["already-dropped"]
    if "nodes" not in tables or "node_edges" not in tables:
        return False, ["nodes/node_edges missing — migration 44 has not run"]

    reasons: list[str] = []

    def unmatched(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    # id-anchored anti-joins (robust to INSERT OR IGNORE dedup; COUNT equality is NOT)
    if "threads" in present and unmatched(
            "SELECT COUNT(*) FROM threads t LEFT JOIN nodes n ON n.id=t.id "
            "WHERE n.id IS NULL"):
        reasons.append("threads rows not mirrored into nodes")
    if "graph_topics" in present and unmatched(
            "SELECT COUNT(*) FROM graph_topics g LEFT JOIN nodes n ON n.id=g.id "
            "WHERE n.id IS NULL"):
        reasons.append("graph_topics rows not mirrored into nodes")
    if "graph_tasks" in present and unmatched(
            "SELECT COUNT(*) FROM graph_tasks g LEFT JOIN nodes n ON n.id=g.id "
            "WHERE n.id IS NULL"):
        reasons.append("graph_tasks rows not mirrored into nodes")
    if "graph_edges" in present and unmatched(
            "SELECT COUNT(*) FROM graph_edges e LEFT JOIN node_edges ne "
            "ON ne.node_id=e.task_id AND ne.depends_on_id=e.depends_on_id "
            "WHERE ne.node_id IS NULL"):
        reasons.append("graph_edges not mirrored into node_edges")

    if conn.execute("SELECT 1 FROM nodes WHERE title IS NULL LIMIT 1").fetchone():
        reasons.append("nodes with NULL title")
    if conn.execute(
            "SELECT 1 FROM nodes c WHERE c.parent_id IS NOT NULL AND "
            "NOT EXISTS (SELECT 1 FROM nodes p WHERE p.id=c.parent_id) LIMIT 1"
    ).fetchone():
        reasons.append("nodes with unresolvable parent_id")
    return (len(reasons) == 0), reasons


# ── Gate A: static source-ref scanner ─────────────────────────────────────────

_GATE = re.compile(
    r"(FROM|JOIN|INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE(\s+IF\s+NOT\s+EXISTS)?|"
    r"DROP\s+TABLE(\s+IF\s+EXISTS)?|REFERENCES)\s+(threads|graph_topics|graph_tasks)\b",
    re.IGNORECASE)


def _excluded(path: Path) -> bool:
    name = path.name
    return (
        name == "p8_readiness.py"
        or (path.parent.name == "dbops"
            and (name.startswith("schema") or name.startswith("migration")))
    )


@dataclass
class LegacyRef:
    file: Path
    line: int
    text: str


def scan_legacy_refs(src_root: Path) -> list[LegacyRef]:
    """Gate A: live steady-state lines still targeting a legacy table.
    Excludes schema/migration modules + p8_readiness + comment-only lines."""
    out: list[LegacyRef] = []
    for py in sorted(Path(src_root).rglob("*.py")):
        if _excluded(py):
            continue
        for i, raw in enumerate(py.read_text(errors="replace").splitlines(), 1):
            stripped = raw.lstrip()
            if stripped.startswith("#"):   # comment-only line
                continue
            if _GATE.search(raw):
                out.append(LegacyRef(py, i, raw.strip()))
    return out


# ── Gate A+B: combined report ─────────────────────────────────────────────────

def pre_p8_report(conn: sqlite3.Connection, src_root: Path) -> dict:
    """Combined static + runtime readiness report (the doctor --pre-p8-check
    payload). `pass` is True iff the static gate is clear AND the runtime gate is
    ready (or the legacy tables are already dropped)."""
    refs = scan_legacy_refs(src_root)
    ready, reasons = p8_drop_ready(conn)
    already = reasons == ["already-dropped"]
    return {
        "static": {"fail": len(refs),
                   "refs": [{"file": str(r.file), "line": r.line, "text": r.text}
                            for r in refs]},
        "runtime": {"ready": ready, "already_dropped": already, "reasons": reasons},
        "pass": (len(refs) == 0 and (ready or already)),
    }
