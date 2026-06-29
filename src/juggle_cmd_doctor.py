"""Juggle CLI — `doctor` subcommand: migrate config + DB to current schema."""

import copy
import json
import os
import shutil
import sqlite3
from pathlib import Path


# Home-default config/backup locations. These double as the module-level
# CONFIG_PATH/BACKUP_PATH seam that existing tests monkeypatch (doc.CONFIG_PATH).
_HOME_CONFIG_DEFAULT = Path.home() / ".juggle" / "config.json"
_HOME_BACKUP_DEFAULT = Path.home() / ".juggle" / "config.json.bak-pre-1.21"

# Initialized to the home default so tests that DON'T patch these still observe
# the home path, which cmd_doctor reads as "no test override" (-> use the
# _JUGGLE_CONFIG_PATH-aware resolver). A test that patches CONFIG_PATH to a tmp
# file makes it differ from the default, which is detected as an override.
CONFIG_PATH = _HOME_CONFIG_DEFAULT
BACKUP_PATH = _HOME_BACKUP_DEFAULT


def _resolve_config_path() -> Path:
    """Resolve the effective config path the SAME way juggle_settings does.

    Parity with juggle_settings.get_settings(): honor _JUGGLE_CONFIG_PATH so
    doctor inspects/prunes the file the runtime actually loads. Without this, a
    deployment whose config comes from _JUGGLE_CONFIG_PATH would have doctor
    silently mutate the home config instead (Codex review, 2026-06-20).
    """
    return Path(os.environ.get("_JUGGLE_CONFIG_PATH", str(_HOME_CONFIG_DEFAULT)))


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


def _clear_settings_cache() -> None:
    """Clear the settings cache if get_settings happens to be lru_cached.

    get_settings is currently uncached (re-reads on every call), so this is a
    best-effort guard — AttributeError just means there is nothing to clear.
    """
    try:
        from juggle_settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except AttributeError:
        pass


def _check_stale_config(dry: bool, cfg_path: Path, backup_path: Path) -> None:
    """Report stale/inert/unknown config keys; prune inert keys when not dry.

    Operates on the EFFECTIVE config/backup paths resolved by cmd_doctor (which
    honor _JUGGLE_CONFIG_PATH), passed in explicitly rather than read from module
    globals — so the prune targets the file the runtime actually loads and the
    backup lands next to it. Reloads the (possibly just-migrated) on-disk config,
    analyzes it against the live DEFAULTS schema, prints the report on every run,
    and in non-dry-run backs up + prunes the inert (safe-to-remove) keys. A
    malformed config is skipped gracefully so doctor never crashes here.
    """
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return  # unreadable/corrupt — already surfaced earlier; skip gracefully
    if not isinstance(cfg, dict):
        return

    import juggle_config_doctor as cdoc
    from juggle_settings import DEFAULTS

    report = cdoc.analyze_config(cfg, DEFAULTS)
    for line in cdoc.format_report(report):
        print(line)

    if not report.prunable_paths:
        return
    if dry:
        print(
            f"stale config: would prune {len(report.prunable_paths)} "
            "inert key(s) (dry-run — no write)"
        )
        return

    if not backup_path.exists():
        shutil.copy2(cfg_path, backup_path)
    new_cfg, removed = cdoc.prune_config(cfg, report.prunable_paths)
    cfg_path.write_text(json.dumps(new_cfg, indent=2))
    _clear_settings_cache()
    print(
        f"stale config: pruned {len(removed)} inert key(s): "
        f"{', '.join(removed)} (backup: {backup_path})"
    )


def cmd_doctor(args) -> int:
    dry = getattr(args, "dry_run", False)
    # Gate A+B readiness report (P8 legacy-table drop prep). Read-only; runs in
    # BOTH dry & non-dry. Extracted to keep this module under the LOC gate.
    if getattr(args, "pre_p8_check", False):
        from juggle_cmd_doctor_p8 import run_pre_p8_check
        return run_pre_p8_check(getattr(args, "json_out", False))
    print(f"juggle doctor — dry_run={dry}")

    # Resolve the EFFECTIVE config/backup paths once. Parity with
    # juggle_settings.get_settings(): honor _JUGGLE_CONFIG_PATH so doctor
    # inspects/prunes the file the runtime loads (Codex review, 2026-06-20).
    #
    # Test-seam override detection: existing tests monkeypatch doc.CONFIG_PATH /
    # doc.BACKUP_PATH to a tmp file. If the module value still equals the home
    # default, no test patched it -> use the _JUGGLE_CONFIG_PATH-aware resolver.
    # If it differs, a test overrode it -> honor the patched value verbatim. The
    # backup is derived from the EFFECTIVE config path so it lands next to the
    # file being pruned, not in $HOME.
    cfg_path = CONFIG_PATH if CONFIG_PATH != _HOME_CONFIG_DEFAULT else _resolve_config_path()
    backup_path = (
        BACKUP_PATH
        if BACKUP_PATH != _HOME_BACKUP_DEFAULT
        else cfg_path.with_name(cfg_path.name + ".bak-pre-1.21")
    )

    # 1. Config
    if cfg_path.exists():
        original = json.loads(cfg_path.read_text())
        new_cfg, changes = _migrate_config(dict(original))
        if changes:
            if not dry:
                if not backup_path.exists():
                    shutil.copy2(cfg_path, backup_path)
                cfg_path.write_text(json.dumps(new_cfg, indent=2))
                _clear_settings_cache()
            print(f"config: {len(changes)} change(s):")
            for c in changes:
                print(f"  - {c}")
            print(f"  backup: {backup_path}" if not dry else "  (dry-run — no write)")
        else:
            print("config: already on 1.21.0 schema")

        # 1b. Stale-config pass — runs on EVERY invocation (even when the
        # migration above made no changes). Detects inert keys (code no longer
        # honors them, e.g. integrate.* since the v1.80.0 full-suite directive)
        # and unknown keys (typos / removed options). Prunes ONLY inert keys in
        # non-dry-run; unknown keys are report-only.
        _check_stale_config(dry, cfg_path, backup_path)
    else:
        print(f"config: {cfg_path} does not exist — nothing to migrate")

    # 2. DB (presence-based; juggle has no schema_version table)
    from juggle_db import JuggleDB, DB_PATH  # noqa: PLC0415 — call-time so tests can patch juggle_db.DB_PATH

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
            # selfheal-v2 P1 (spec §6): quiesce the watchdog before any rebuild so
            # no writer races the error_events table swap (Migration 45).
            # stop_watchdog is a no-op if none is running; the 30s backstop
            # relaunches it after migration.
            try:
                from juggle_watchdog_singleton import stop_watchdog
                if stop_watchdog(DB_PATH):
                    print("db: quiesced watchdog for safe migration")
            except Exception as e:  # never let quiesce failure mask the migration
                print(f"db: watchdog quiesce skipped ({e})")
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

    # 3.5 Re-link nodes.parent_id from legacy graph_tasks + resync drifted state
    # BEFORE topic reconcile (it reads parent_id). DEFECT #4907; idempotent repair.
    if not dry:
        try:
            from dbops.migration_parent_relink import parent_reconcile_summary
            print(parent_reconcile_summary(JuggleDB(DB_PATH)))
        except Exception as e:
            print(f"graph parentage: skipped ({e})")

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

    # 4.5 Backfill merged_sha for out-of-band merges (orphan-guard
    # false-positive fix, 2026-06-17). A topic whose work was merged outside
    # `juggle integrate` is reachable from main but has a NULL merged_sha, so the
    # orphan guard re-fires a HIGH alert every tick. Stamp merged_sha from the
    # already-merged branch and reconcile. Idempotent; orchestrator-only (G2-safe).
    if not dry:
        try:
            from dbops.orphan_guard import reconcile_out_of_band_merges
            reconciled = reconcile_out_of_band_merges(db_instance)
            if reconciled:
                print(f"merged-sha backfill: {len(reconciled)} topic(s) "
                      f"reconciled ({', '.join(reconciled)})")
            else:
                print("merged-sha backfill: nothing to reconcile")
        except Exception as e:
            print(f"merged-sha backfill: skipped ({e})")
    else:
        print("merged-sha backfill: (dry-run — skipped)")

    # P8 (Task 4.2): the graph-mirrors-threads backfill is RETIRED — a conversation
    # is now a first-class kind='conversation' node, not a graph_topics projection,
    # so there is no mirror to backfill.

    return 0
