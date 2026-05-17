#!/usr/bin/env python3
"""
Measure agent behavioral compliance for the prompt injection spec (v1.20.0+).

Usage:
    python3 scripts/measure_agent_compliance.py --mode baseline
    python3 scripts/measure_agent_compliance.py --mode post-deploy [--days 21] [--n 15]

Output: CSV to stdout
  thread_uuid,role,date,metric_name,value

value is 1 (compliant), 0 (non-compliant), or empty (file not found / not checkable).

Role detection uses topic prefix heuristics + last assistant message content.
  researcher: topic starts with "research-" OR last message contains "Research complete"
  planner:    topic starts with "plan-" OR last message references a .md in the plan dir
  coder:      everything else with a non-empty last assistant message

Compliance checks:
  researcher: has_confidence_markers  — any [HIGH CONFIDENCE]/[CONFLICTING]/[UNVERIFIED] in report file
              has_gaps_section        — ^## Gaps line in report file
  coder:      has_quality_gate        — last message mentions pre-pr|quality.gate|lint|tests pass
  planner:    has_da_section          — ^## Devil's Advocate in referenced plan file
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _get_db_path() -> Path:
    from juggle_settings import get_settings
    return Path(get_settings()["paths"]["data_dir"]) / "juggle.db"


# ---------------------------------------------------------------------------
# Role inference
# ---------------------------------------------------------------------------

def _infer_role(topic: str, last_msg: str) -> str | None:
    t = (topic or "").lower()
    m = (last_msg or "").lower()
    if t.startswith("research-") or m.startswith("research complete"):
        return "researcher"
    plan_dir = str(Path.home() / "Documents/personal/projects/juggle/plan/")
    if t.startswith("plan-") or plan_dir.lower() in m or "devil's advocate" in m:
        return "planner"
    if last_msg and last_msg.strip():
        return "coder"
    return None


# ---------------------------------------------------------------------------
# File-path extraction
# ---------------------------------------------------------------------------

def _extract_researcher_file(last_msg: str) -> Path | None:
    """Pull the saved .md path from 'Research complete: <path>'."""
    m = re.search(r"(?:Research complete[:\s]+)([^\s,]+\.md)", last_msg or "", re.IGNORECASE)
    if not m:
        return None
    raw = m.group(1)
    p = Path(raw) if raw.startswith("/") else Path.home() / "Documents/personal" / raw
    return p


def _extract_plan_file(last_msg: str) -> Path | None:
    """Pull the first .md reference from a planner completion message."""
    m = re.search(r"([^\s'\"]+\.md)", last_msg or "")
    if not m:
        return None
    raw = m.group(1)
    p = Path(raw) if raw.startswith("/") else Path(raw)
    if not p.exists():
        # Try plan dir
        plan_dir = Path.home() / "Documents/personal/projects/juggle/plan"
        p = plan_dir / p.name
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Compliance checkers
# ---------------------------------------------------------------------------

def _check_researcher(last_msg: str) -> dict:
    path = _extract_researcher_file(last_msg)
    if not path or not path.exists():
        return {"has_confidence_markers": None, "has_gaps_section": None}
    text = path.read_text(errors="replace")
    markers = bool(re.search(r"\[HIGH CONFIDENCE\]|\[CONFLICTING\]|\[UNVERIFIED\]", text))
    gaps = bool(re.search(r"^## Gaps", text, re.MULTILINE))
    return {"has_confidence_markers": markers, "has_gaps_section": gaps}


def _check_coder(last_msg: str) -> dict:
    text = (last_msg or "").lower()
    has_gate = bool(re.search(
        r"pre.?pr|quality.gate|linting|lint|tests?\s+pass|pytest|ruff|mypy|type.?error",
        text,
    ))
    return {"has_quality_gate": has_gate}


def _check_planner(last_msg: str) -> dict:
    path = _extract_plan_file(last_msg)
    if not path:
        return {"has_da_section": None}
    text = path.read_text(errors="replace")
    has_da = bool(re.search(r"^## Devil'?s Advocate", text, re.MULTILINE | re.IGNORECASE))
    return {"has_da_section": has_da}


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

def _fetch_threads(db_path: Path, cutoff_date: str) -> list[dict]:
    """Return archived/closed threads with their last assistant message, newest first."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            t.id,
            t.topic,
            COALESCE(t.last_active_at, t.last_active) AS active_at,
            m.content                                   AS last_msg
        FROM threads t
        JOIN (
            SELECT thread_id, content
            FROM   messages
            WHERE  role = 'assistant'
              AND  id IN (
                      SELECT MAX(id)
                      FROM   messages
                      WHERE  role = 'assistant'
                      GROUP BY thread_id
                   )
        ) m ON m.thread_id = t.id
        WHERE t.status IN ('archived', 'closed')
          AND COALESCE(t.last_active_at, t.last_active) >= ?
        ORDER BY COALESCE(t.last_active_at, t.last_active) DESC
    """, (cutoff_date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Measure agent behavioral compliance (CSV output)")
    ap.add_argument("--mode", choices=["baseline", "post-deploy"], required=True,
                    help="baseline: last 30 days; post-deploy: last 21 days")
    ap.add_argument("--days", type=int, default=None,
                    help="Override look-back window (days)")
    ap.add_argument("--n", type=int, default=15,
                    help="Max threads per role (default: 15)")
    args = ap.parse_args()

    days = args.days if args.days is not None else (30 if args.mode == "baseline" else 21)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    db_path = _get_db_path()

    if not db_path.exists():
        sys.exit(f"DB not found: {db_path}")

    threads = _fetch_threads(db_path, cutoff)

    writer = csv.writer(sys.stdout)
    writer.writerow(["thread_uuid", "role", "date", "metric_name", "value"])

    seen: dict[str, int] = {"researcher": 0, "coder": 0, "planner": 0}

    for row in threads:
        role = _infer_role(row["topic"], row["last_msg"])
        if role not in seen:
            continue
        if seen[role] >= args.n:
            continue
        seen[role] += 1

        date = (row["active_at"] or "")[:10]
        tid = row["id"]

        if role == "researcher":
            checks = _check_researcher(row["last_msg"])
        elif role == "coder":
            checks = _check_coder(row["last_msg"])
        else:
            checks = _check_planner(row["last_msg"])

        for metric, val in checks.items():
            csv_val = "" if val is None else ("1" if val else "0")
            writer.writerow([tid, role, date, metric, csv_val])

    # Summary to stderr so it doesn't pollute CSV stdout
    total = sum(seen.values())
    print(
        f"# mode={args.mode}  window={days}d  cutoff={cutoff}  "
        f"threads: researcher={seen['researcher']} coder={seen['coder']} planner={seen['planner']}  "
        f"total={total}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
