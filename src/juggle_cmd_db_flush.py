"""juggle_cmd_db_flush — flush live (tmpfs) DB to durable disk path.

Commands:
  juggle db-flush           — loop, flushing every flush_interval_s (daemon)
  juggle db-flush --once    — single flush, then exit (deterministic; used in tests)
  juggle db-flush --status  — print JSON: {last_flush_at, age_s}
  juggle db-flush --install-supervisor — write systemd unit / launchd plist

Flush protocol:
  1. sqlite3.Connection.backup(dst) live → durable.tmp
  2. os.replace(durable.tmp, durable)  — atomic rename
  Interrupted flush leaves durable intact.

Status tracking: a sidecar file `<durable>.flush-ts` stores the ISO timestamp
of the last successful flush. flush_status() reads it.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_TIMESTAMP_SUFFIX = ".flush-ts"


def _ts_path(durable: Path) -> Path:
    return Path(str(durable) + _TIMESTAMP_SUFFIX)


def flush_once(live: Path, durable: Path) -> None:
    """Flush live → durable atomically.

    Uses sqlite3 backup API for consistency, then atomic rename.
    """
    live = Path(live)
    durable = Path(durable)
    tmp = Path(str(durable) + ".tmp")

    durable.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(live))
    dst = sqlite3.connect(str(tmp))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    os.replace(tmp, durable)

    # Record flush timestamp
    _ts_path(durable).write_text(
        datetime.now(timezone.utc).isoformat()
    )


def flush_status(durable: Path) -> dict:
    """Return {last_flush_at: str|None, age_s: float|None}."""
    ts_file = _ts_path(Path(durable))
    if not ts_file.exists():
        return {"last_flush_at": None, "age_s": None}
    ts_str = ts_file.read_text().strip()
    try:
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        age = (now - ts).total_seconds()
        return {"last_flush_at": ts_str, "age_s": round(age, 1)}
    except ValueError:
        return {"last_flush_at": ts_str, "age_s": None}


def _run_daemon(live: Path, durable: Path, interval: float) -> None:
    """Flush loop — runs until SIGTERM."""
    running = True

    def _handle_term(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _handle_term)

    while running:
        try:
            flush_once(live, durable)
        except Exception as e:
            print(f"[db-flush] flush error: {e}", file=sys.stderr)
        time.sleep(interval)

    # Final flush on shutdown
    try:
        flush_once(live, durable)
    except Exception as e:
        print(f"[db-flush] final flush error: {e}", file=sys.stderr)


def _install_supervisor(live: Path, durable: Path, interval: float) -> None:
    """Write a systemd unit (Linux) or launchd plist (macOS)."""
    import platform
    juggle_cli = Path(sys.argv[0]).resolve()
    sys_platform = platform.system().lower()

    if sys_platform == "linux":
        unit = f"""[Unit]
Description=Juggle DB Flush Daemon
After=network.target

[Service]
Type=simple
ExecStart={juggle_cli} db flush --live {live} --durable {durable} --interval {int(interval)}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        unit_path = Path.home() / ".config" / "systemd" / "user" / "juggle-db-flush.service"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit)
        print(f"Wrote systemd unit: {unit_path}")
        print("Enable with: systemctl --user enable --now juggle-db-flush")
    else:
        label = "com.juggle.dbflush"
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{juggle_cli}</string>
    <string>db</string>
    <string>flush</string>
    <string>--live</string><string>{live}</string>
    <string>--durable</string><string>{durable}</string>
    <string>--interval</string><string>{int(interval)}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>"""
        agents_dir = Path.home() / "Library" / "LaunchAgents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        # Remove the legacy-labelled unit so no on-disk plist references db-flush.
        (agents_dir / "com.juggle.db-flush.plist").unlink(missing_ok=True)
        plist_path = agents_dir / f"{label}.plist"
        plist_path.write_text(plist)
        print(f"Wrote launchd plist: {plist_path}")
        print(f"Load with: launchctl load {plist_path}")


def configure_db_mode(mode: str, *, config_path: Path | None = None) -> None:
    """Write db.mode to config.json idempotently.

    Used by juggle:init to persist the chosen DB mode.
    Preserves all other keys in the config file.
    """
    import json
    import os
    if config_path is None:
        config_path = Path(
            os.environ.get("_JUGGLE_CONFIG_PATH",
                           str(Path.home() / ".juggle" / "config.json"))
        )
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            cfg = {}
    db_section = cfg.setdefault("db", {})
    db_section["mode"] = mode
    config_path.write_text(json.dumps(cfg, indent=2))


def cmd_db_flush(args) -> int:
    """Entry point for `juggle db-flush`."""
    from juggle_settings import get_settings
    from dbops.schema import _resolve_db_path

    settings = get_settings()
    db_cfg = settings.get("db", {})
    mode = db_cfg.get("mode", "direct")
    tmpfs_dir = db_cfg.get("tmpfs_dir", "/dev/shm")
    interval = float(db_cfg.get("flush_interval_s", 10))

    durable = _resolve_db_path()

    # Override from args if provided
    live_arg = getattr(args, "live", None)
    durable_arg = getattr(args, "durable", None)
    interval_arg = getattr(args, "interval", None)
    if interval_arg is not None:
        interval = float(interval_arg)

    if durable_arg:
        durable = Path(durable_arg)

    if live_arg:
        live = Path(live_arg)
    elif mode == "tmpfs":
        from juggle_db_path import resolve_db_paths
        instance_id = os.environ.get("JUGGLE_INSTANCE_ID", "default")
        paths = resolve_db_paths("tmpfs", tmpfs_dir, durable, instance_id)
        live = paths.live
    else:
        live = durable

    if getattr(args, "status", False):
        status = flush_status(durable)
        print(json.dumps(status))
        return 0

    if getattr(args, "install_supervisor", False):
        _install_supervisor(live, durable, interval)
        return 0

    if getattr(args, "once", False):
        flush_once(live, durable)
        print(f"[db-flush] flushed {live} → {durable}")
        return 0

    # Default: daemon loop
    _run_daemon(live, durable, interval)
    return 0
