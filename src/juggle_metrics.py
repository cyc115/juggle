"""juggle_metrics — pure cost/perf/quality aggregations over agent_runs rows
(2026-06-30 orchestration-metrics §3). Deterministic; no LLM. quality() joins
nodes/error_events via the db for the error rate. NEVER mutates its inputs."""
from __future__ import annotations

from datetime import datetime, timezone

import juggle_metrics_errors as _errors
from juggle_prompt_metrics import boilerplate_bytes


def _parse(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _i(r, k) -> int:
    return int(r.get(k) or 0)


def _io(r) -> int:
    return _i(r, "input_tokens") + _i(r, "output_tokens")


def _all_tokens(r) -> int:
    return _io(r) + _i(r, "cache_read_tokens") + _i(r, "cache_write_tokens")


def _mean(nums) -> float:
    nums = list(nums)
    return round(sum(nums) / len(nums), 6) if nums else 0.0


def _secs(r):
    a, b = _parse(r.get("dispatched_at")), _parse(r.get("completed_at"))
    return (b - a).total_seconds() if (a and b) else None


def _first_per_agent(runs: list[dict]) -> list[dict]:
    """The earliest (MIN dispatched_at) run per agent_id — the cold-start set."""
    first: dict = {}
    for r in runs:
        aid = r.get("agent_id")
        if aid is None:
            continue
        cur = first.get(aid)
        if cur is None or (r.get("dispatched_at") or "") < (cur.get("dispatched_at") or ""):
            first[aid] = r
    return list(first.values())


def cost(runs: list[dict]) -> dict:
    verified = [r for r in runs if (r.get("status") or "") == "completed"]
    n_v = len(verified)
    bp = sum(boilerplate_bytes(r.get("input_prompt") or "", preamble=_preamble()) for r in runs)
    pbytes = sum(_i(r, "prompt_bytes") for r in runs)
    cold = _first_per_agent(runs)
    return {
        "tokens_per_dispatch": _mean(_io(r) for r in runs),
        "input_per_dispatch": _mean(_i(r, "input_tokens") for r in runs),
        "output_per_dispatch": _mean(_i(r, "output_tokens") for r in runs),
        "cache_per_dispatch": _mean(_i(r, "cache_read_tokens") + _i(r, "cache_write_tokens") for r in runs),
        "tokens_per_verified": round(sum(_io(r) for r in verified) / n_v, 6) if n_v else 0.0,
        "boilerplate_share": round(bp / pbytes, 6) if pbytes else 0.0,
        "cold_start_tokens": _mean(_io(r) for r in cold),
        "token_coverage": round(100 * sum(1 for r in verified if _all_tokens(r) > 0) / n_v, 6) if n_v else 0.0,
    }


def performance(runs: list[dict]) -> dict:
    walls = [s for s in (_secs(r) for r in runs) if s is not None]
    cold = _first_per_agent(runs)
    cold_walls = [s for s in (_secs(r) for r in cold) if s is not None]
    starts = [_parse(r.get("dispatched_at")) for r in runs if _parse(r.get("dispatched_at"))]
    ends = [_parse(r.get("completed_at")) for r in runs if _parse(r.get("completed_at"))]
    span = (max(ends) - min(starts)).total_seconds() if (starts and ends) else 0.0
    busy = sum(s for s in walls)
    return {
        "wall_per_dispatch_secs": _mean(walls),
        "cold_start_latency_secs": _mean(cold_walls),
        "parallelism_factor": round(busy / span, 6) if span > 0 else 0.0,
        "queue_wait_secs": None,  # deferred — OQ-M2 (node transitions not timestamped)
    }


def quality(db, runs: list[dict]) -> dict:
    dispatched = len(runs)
    verified = [r for r in runs if (r.get("status") or "") == "completed"]
    n_v = len(verified)
    # first-pass: a verified run whose task_id had NO failed run at an earlier id.
    failed_by_task: dict = {}
    for r in runs:
        if (r.get("status") or "") == "failed":
            failed_by_task.setdefault(r.get("task_id"), []).append(_i(r, "id"))
    first_pass = 0
    for r in verified:
        earlier = [fid for fid in failed_by_task.get(r.get("task_id"), []) if fid < _i(r, "id")]
        if not earlier:
            first_pass += 1
    counts: dict = {}
    for r in runs:
        tid = r.get("task_id")
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    distinct = len(counts)
    reworked = sum(1 for c in counts.values() if c > 1)
    return {
        "completion_pct": round(100 * n_v / dispatched, 6) if dispatched else 0.0,
        "first_pass_yield": round(100 * first_pass / dispatched, 6) if dispatched else 0.0,
        "rework_rate": round(reworked / distinct, 6) if distinct else 0.0,
        "cost_to_green": round(sum(_io(r) for r in verified) / n_v, 6) if n_v else 0.0,
        "error_rate": _errors.error_breakdown(db, runs)["rate"],
    }


def compute_metrics(db, runs: list[dict]) -> dict:
    return {"cost": cost(runs), "performance": performance(runs),
            "quality": quality(db, runs)}


def _preamble() -> str:
    try:
        from juggle_cmd_agents_common import UNIVERSAL_PREAMBLE
        return UNIVERSAL_PREAMBLE
    except Exception:
        return ""
