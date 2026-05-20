"""Juggle CLI — `doctor` subcommand: migrate config + DB to current schema."""

import copy
import json
import shutil
import sqlite3
from pathlib import Path


CONFIG_PATH = Path.home() / ".juggle" / "config.json"
BACKUP_PATH = Path.home() / ".juggle" / "config.json.bak-pre-1.21"


def _migrate_config(cfg: dict) -> tuple[dict, list[str]]:
    """Pure helper: rewrite a config dict from the pre-1.21.0 schema to 1.21+.

    Returns (new_cfg, list_of_change_descriptions).
    """
    cfg = copy.deepcopy(cfg)
    changes: list[str] = []
    domains = cfg.get("domains")
    if not isinstance(domains, dict):
        return cfg, changes

    paths = cfg.setdefault("paths", {})

    if "vault" not in paths:
        initial_paths = domains.get("initial_domain_paths") or []
        for entry in initial_paths:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) >= 2
                and entry[1] == "vault"
            ):
                paths["vault"] = entry[0]
                changes.append(
                    f"paths.vault = {entry[0]} (migrated from domains.initial_domain_paths)"
                )
                break

    if "vault_name" not in paths:
        legacy_name = domains.get("vault_name", "")
        if legacy_name:
            paths["vault_name"] = legacy_name
            changes.append(
                f"paths.vault_name = {legacy_name} (migrated from domains.vault_name)"
            )

    del cfg["domains"]
    changes.append("removed obsolete domains block")
    return cfg, changes


def cmd_doctor(args) -> int:
    dry = getattr(args, "dry_run", False)
    print(f"juggle doctor — dry_run={dry}")

    # 1. Config
    if CONFIG_PATH.exists():
        original = json.loads(CONFIG_PATH.read_text())
        new_cfg, changes = _migrate_config(dict(original))
        if changes:
            if not dry:
                if not BACKUP_PATH.exists():
                    shutil.copy2(CONFIG_PATH, BACKUP_PATH)
                CONFIG_PATH.write_text(json.dumps(new_cfg, indent=2))
                from juggle_settings import get_settings

                get_settings.cache_clear()
            print(f"config: {len(changes)} change(s):")
            for c in changes:
                print(f"  - {c}")
            print(f"  backup: {BACKUP_PATH}" if not dry else "  (dry-run — no write)")
        else:
            print("config: already on 1.21.0 schema")
    else:
        print(f"config: {CONFIG_PATH} does not exist — nothing to migrate")

    # 2. DB (presence-based; juggle has no schema_version table)
    from juggle_db import JuggleDB, DB_PATH

    if Path(DB_PATH).exists():
        conn = sqlite3.connect(str(DB_PATH))
        thread_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        stale = (
            "domain" in thread_cols or "domains" in tables or "domain_paths" in tables
        )
        if stale:
            if not dry:
                JuggleDB(DB_PATH).init_db()
                print(
                    "db: ran Migrations 17–19 (dropped domain column + domain tables)"
                )
            else:
                print("db: would run Migrations 17–19 (stale schema detected)")
        else:
            print("db: schema already on 1.21.0")
    else:
        print(f"db: {DB_PATH} does not exist — will be created on first juggle command")

    return 0
