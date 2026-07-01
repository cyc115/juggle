"""juggle_run_tokens — read per-run Claude Code usage tokens from the agent's
transcript, window-summed over [dispatched_at, completed_at] (2026-06-30
orchestration-metrics Task 0). Pooled panes share one transcript, so per-run
attribution is a timestamp-window sum, not a session total. NEVER raises."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("juggle-run-tokens")
_ZERO = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
_DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def project_dir_for_cwd(cwd: str) -> str:
    """Claude Code project-dir name: every '/' and '.' in the cwd -> '-'."""
    return re.sub(r"[/.]", "-", cwd)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def sum_usage_in_window(jsonl_path: Path, start: datetime, end: datetime) -> dict:
    acc = dict(_ZERO)
    try:
        text = jsonl_path.read_text(errors="replace")
    except OSError:
        return acc
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "assistant":
            continue
        ts = _parse_ts(rec.get("timestamp"))
        if ts is None or ts < start or ts > end:
            continue
        u = (rec.get("message") or {}).get("usage") or {}
        acc["input"] += int(u.get("input_tokens") or 0)
        acc["output"] += int(u.get("output_tokens") or 0)
        acc["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        acc["cache_write"] += int(u.get("cache_creation_input_tokens") or 0)
    return acc


def read_run_tokens(run: dict, *, projects_root: Path | None = None) -> dict:
    """Sum transcript usage for a run's window. NEVER raises."""
    try:
        root = projects_root or _DEFAULT_ROOT
        start = _parse_ts(run.get("dispatched_at"))
        end = _parse_ts(run.get("completed_at"))
        cwd = run.get("repo_path")
        if not (start and end and cwd):
            return dict(_ZERO)
        d = root / project_dir_for_cwd(cwd)
        sid = run.get("session_id")
        files = [d / f"{sid}.jsonl"] if sid else sorted(d.glob("*.jsonl"))  # Tier1 pin / Tier2 glob
        total = dict(_ZERO)
        for f in files:
            if not f.exists():
                continue
            part = sum_usage_in_window(f, start, end)
            for k in total:
                total[k] += part[k]
        return total
    except Exception:
        _log.exception("read_run_tokens failed — returning zeros")
        return dict(_ZERO)
