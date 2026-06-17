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
    from juggle_db import JuggleDB
    from dbops.schema import _resolve_db_path
    DB_PATH = _resolve_db_path()  # call-time resolution respects JUGGLE_DB_PATH

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
        # Migration 39 (node->task rename): a node-era DB still carries the old
        # graph_nodes table; init_db's run_migrations reconciles it into graph_tasks.
        node_era = "graph_nodes" in tables
        legacy_notes = []
        if stale:
            legacy_notes.append("Migrations 17–19 (dropped domain column + domain tables)")
        if node_era:
            legacy_notes.append("Migration 39 (graph_nodes -> graph_tasks rename)")

        # Always run the idempotent migration pass so new additive migrations
        # (e.g. Migration 42 graph_topics.is_mirror) apply even when the base
        # schema already looks current. Each migration self-guards against
        # duplicate-column errors.
        if not dry:
            JuggleDB(DB_PATH).init_db()
            if legacy_notes:
                print(f"db: ran {'; '.join(legacy_notes)}")
            print("db: ran idempotent migration pass")
        else:
            msg = "; ".join(legacy_notes) if legacy_notes else "idempotent migration pass"
            print(f"db: would run {msg}")
    else:
        print(f"db: {DB_PATH} does not exist — will be created on first juggle command")
        return 0

    # 3. Slug persistence (T-slug-wheel): slugs are PERMANENT historical handles
    # and must NOT be nulled on close/archive. The old recycling-by-erasure
    # backfill was removed; reuse is handled by the wheel's skip-live rule plus
    # the partial unique index idx_threads_live_label. Nothing to backfill.

    # 4. Reconcile graph topic states (repair drift between task tier + topic tier)
    from dbops import db_topics as dbt

    db_instance = JuggleDB(DB_PATH)
    try:
        projects = db_instance.list_projects(include_archived=True)
    except Exception:
        projects = []
    fixed = 0
    for project in projects:
        try:
            result = dbt.reconcile_project_topics(db_instance, project["id"])
        except Exception:
            continue
        for tid, info in result.items():
            if info["before"] != info["after"]:
                print(f"graph reconcile: {tid}: {info['before']} → {info['after']}")
                fixed += 1
    if fixed:
        print(f"graph reconcile: {fixed} topic(s) repaired")
    else:
        print("graph reconcile: all topics consistent")

    # 5. Backfill mirror topics (graph-mirrors-threads, 2026-06-14).
    # Idempotent: safe to run on every doctor invocation. G2-safe: doctor runs
    # as the orchestrator, never from an agent/worktree context.
    if not dry:
        try:
            from dbops.db_mirror import backfill_mirror_topics
            n_mirrors = backfill_mirror_topics(db_instance)
            print(f"mirror backfill: {n_mirrors} thread(s) processed")
        except Exception as e:
            print(f"mirror backfill: skipped ({e})")
    else:
        print("mirror backfill: (dry-run — skipped)")

    return 0
