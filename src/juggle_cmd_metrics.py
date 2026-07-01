"""juggle_cmd_metrics — `juggle metrics` CLI (2026-06-30 orchestration-metrics).
Read-only rollup over agent_runs: load → slice (--by) → compute cost/perf/quality
per slice. `--json` first, human table second. No LLM; deterministic."""
from __future__ import annotations

import json

from juggle_cli_common import get_db
from juggle_metrics import compute_metrics
from juggle_metrics_load import group_runs, load_runs

# Headline numbers surfaced in the human table (JSON carries the full dict).
_HEADLINE = [
    ("cost", "tokens_per_dispatch"),
    ("cost", "tokens_per_verified"),
    ("cost", "boilerplate_share"),
    ("cost", "token_coverage"),
    ("performance", "wall_per_dispatch_secs"),
    ("quality", "completion_pct"),
    ("quality", "first_pass_yield"),
    ("quality", "error_rate"),
]

_NOTE = ("error_rate is a Phase-1 UNDER-count (failed+rework+blocked+selfheal_classb; "
         "stale-reset/wrong-target deferred). token_coverage = % completed runs with "
         "nonzero tokens; low coverage means the token pipeline is incomplete.")


def cmd_metrics(args) -> None:
    _db_path = getattr(args, "db_path", None)
    db = get_db(db_path=_db_path) if isinstance(_db_path, str) else get_db()
    since = getattr(args, "since", None)
    by = getattr(args, "by", None)
    runs = load_runs(db, since=since)
    slices = {k: compute_metrics(db, group) for k, group in group_runs(runs, by).items()}
    payload = {"since": since, "by": by, "note": _NOTE, "slices": slices}
    if getattr(args, "json_out", False):
        print(json.dumps(payload, indent=2))
        return
    _print_table(payload)


def _print_table(payload: dict) -> None:
    by = payload.get("by") or "all"
    print(f"orchestration metrics (by={by}, since={payload.get('since') or 'all-time'})")
    cols = [name for _, name in _HEADLINE]
    header = "  ".join([f"{'slice':<16}"] + [f"{c:>20}" for c in cols])
    print(header)
    print("-" * len(header))
    for key, met in payload["slices"].items():
        cells = [f"{str(key)[:16]:<16}"]
        for section, name in _HEADLINE:
            val = met.get(section, {}).get(name)
            cells.append(f"{('' if val is None else val)!s:>20}")
        print("  ".join(cells))
    print(f"\nnote: {payload['note']}")
