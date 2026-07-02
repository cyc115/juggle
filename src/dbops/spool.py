"""dbops.spool — atomic file-based event spool (single-writer broker, P2/T-spool).

Owns: SpoolEvent, write_event (atomic tmp+rename), read_pending (filename-sorted,
skips malformed/dead), move_to_dead. Pure filesystem — no DB import, no juggle_db
dependency, so agent-context CLI processes never need a DB connection to spool.
"""
from __future__ import annotations

import json
import os
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SpoolEvent:
    uuid: str
    type: str
    agent_id: str
    thread_id: str
    args: dict = field(default_factory=dict)
    created_at: str = ""
    path: Path | None = None  # set by read_pending; None for freshly-written events


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%f")


def write_event(spool_dir: Path, event_type: str, agent_id: str, thread_id: str,
                 args: dict) -> str:
    """Atomically write one spool event; returns the event uuid."""
    spool_dir.mkdir(parents=True, exist_ok=True)
    event_uuid = str(_uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "uuid": event_uuid, "type": event_type, "agent_id": agent_id or "",
        "thread_id": thread_id or "", "args": args, "created_at": created_at,
    }
    safe_agent = (agent_id or "noagent").replace("/", "_")[:12]
    name = f"{_utc_ts()}-{safe_agent}-{event_uuid[:8]}.json"
    final_path = spool_dir / name
    tmp_path = spool_dir / f".{name}.tmp"
    tmp_path.write_text(json.dumps(payload))
    os.rename(tmp_path, final_path)  # atomic on the same filesystem
    return event_uuid


def read_pending(spool_dir: Path) -> list[SpoolEvent]:
    """Pending events, oldest-first (filenames sort by timestamp). Malformed
    files are skipped (never raise) — the drain loop dead-letters them by path."""
    if not spool_dir.exists():
        return []
    events: list[SpoolEvent] = []
    for path in sorted(spool_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
            events.append(SpoolEvent(
                uuid=payload["uuid"], type=payload["type"],
                agent_id=payload.get("agent_id", ""),
                thread_id=payload.get("thread_id", ""),
                args=payload.get("args", {}),
                created_at=payload.get("created_at", ""),
                path=path,
            ))
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return events


def move_to_dead(spool_dir: Path, event_path: Path, reason: str) -> None:
    dead_dir = spool_dir / "dead"
    dead_dir.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(event_path.read_text())
    except (json.JSONDecodeError, OSError):
        payload = {"uuid": event_path.stem, "type": "unknown"}
    payload["dead_reason"] = reason
    dest = dead_dir / event_path.name
    dest.write_text(json.dumps(payload))
    event_path.unlink(missing_ok=True)
