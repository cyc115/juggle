#!/usr/bin/env python3
"""Consolidate orphan Juggle DBs into the canonical ~/.claude/juggle/juggle.db.

Root cause: earlier versions of juggle_settings.py honored CLAUDE_PLUGIN_DATA,
causing each plugin context to spawn its own juggle.db. This script merges
orphan DBs into the canonical location.

Safe to run multiple times. Use --dry-run to preview."""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def get_tables(db_path: str) -> List[str]:
    """Get list of table names from database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def get_schema(db_path: str, table: str) -> str:
    """Get CREATE TABLE statement for a table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def normalize_schema(schema: str) -> str:
    """Normalize schema for comparison, ignoring foreign key details and whitespace."""
    if not schema:
        return schema
    import re
    # Remove REFERENCES clauses and their content to ignore FK differences
    normalized = re.sub(r'REFERENCES\s+"[^"]+"\([^)]*\)', 'REFERENCES threads(id)', schema)
    normalized = re.sub(r'REFERENCES\s+\w+\([^)]*\)', 'REFERENCES threads(id)', normalized)
    # Collapse multiple whitespace
    normalized = re.sub(r'\s+', ' ', normalized)
    # Remove leading/trailing whitespace
    normalized = normalized.strip()
    return normalized


def get_primary_key(db_path: str, table: str) -> str:
    """Get primary key column name for a table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = cursor.fetchall()
    conn.close()
    for col in columns:
        if col[5] == 1:  # pk flag
            return col[1]  # column name
    # If no explicit PK found, use first column as fallback
    if columns:
        return columns[0][1]
    return "id"


def get_existing_ids(db_path: str, table: str, pk_col: str) -> set:
    """Get set of existing primary key values in destination table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT {pk_col} FROM {table}")
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids


def get_thread_labels(db_path: str) -> Dict[str, str]:
    """Get mapping of thread UUID to label (active threads only)."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT id, label FROM threads WHERE status != 'archived'")
    labels = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return labels


def get_rows_to_migrate(src_db: str, dst_db: str, table: str, pk_col: str) -> List[Tuple]:
    """Get rows from src that don't exist in dst (by PK)."""
    dst_ids = get_existing_ids(dst_db, table, pk_col)

    conn = sqlite3.connect(src_db)
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table}")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()
    conn.close()

    pk_index = columns.index(pk_col)
    return [row for row in rows if row[pk_index] not in dst_ids]


def consolidate(src_db: str, dst_db: str, dry_run: bool = False, verbose: bool = False) -> Tuple[int, Dict[str, int], int]:
    """Consolidate src DB into dst DB.

    Returns: (total_rows_migrated, per_table_counts, label_collisions)
    """
    src_path = Path(src_db).expanduser()
    dst_path = Path(dst_db).expanduser()

    if not src_path.exists():
        print(f"Error: source DB not found: {src_path}", file=sys.stderr)
        sys.exit(1)

    if not dst_path.exists():
        print(f"Error: destination DB not found: {dst_path}", file=sys.stderr)
        sys.exit(1)

    # Verify schema match
    src_tables = set(get_tables(str(src_path)))
    dst_tables = set(get_tables(str(dst_path)))

    if src_tables != dst_tables:
        print(f"Error: table mismatch. Src: {src_tables}, Dst: {dst_tables}", file=sys.stderr)
        sys.exit(1)

    for table in src_tables:
        src_schema = get_schema(str(src_path), table)
        dst_schema = get_schema(str(dst_path), table)
        # Normalize to ignore foreign key reference differences
        if normalize_schema(src_schema) != normalize_schema(dst_schema):
            print(f"Error: schema mismatch for table '{table}'", file=sys.stderr)
            sys.exit(1)

    # Get thread labels from dst to detect collisions
    dst_labels = get_thread_labels(str(dst_path)) if "threads" in src_tables else {}

    # Migration loop
    total_rows = 0
    per_table = {}
    label_collisions = 0

    dst_conn = sqlite3.connect(str(dst_path))
    dst_cursor = dst_conn.cursor()

    for table in sorted(src_tables):
        pk_col = get_primary_key(str(src_path), table)
        rows_to_migrate = get_rows_to_migrate(str(src_path), str(dst_path), table, pk_col)

        if not rows_to_migrate:
            per_table[table] = 0
            continue

        # Get column names
        src_conn = sqlite3.connect(str(src_path))
        src_cursor = src_conn.cursor()
        src_cursor.execute(f"SELECT * FROM {table} LIMIT 0")
        columns = [desc[0] for desc in src_cursor.description]
        src_conn.close()

        # Handle label collisions for threads
        if table == "threads":
            label_col_index = columns.index("label") if "label" in columns else -1
            status_col_index = columns.index("status") if "status" in columns else -1
            id_col_index = columns.index("id") if "id" in columns else 0

            rows_to_migrate = list(rows_to_migrate)
            for i, row in enumerate(rows_to_migrate):
                if label_col_index >= 0 and row[label_col_index] in dst_labels.values():
                    # Collision detected
                    label_collisions += 1
                    row_list = list(row)
                    if status_col_index >= 0:
                        row_list[status_col_index] = "archived"
                    # Also set show_in_list to 0 if it exists
                    if "show_in_list" in columns:
                        show_in_list_index = columns.index("show_in_list")
                        row_list[show_in_list_index] = 0
                    rows_to_migrate[i] = tuple(row_list)

        if not dry_run:
            placeholders = ",".join(["?" for _ in columns])
            insert_sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
            for row in rows_to_migrate:
                dst_cursor.execute(insert_sql, row)

        per_table[table] = len(rows_to_migrate)
        total_rows += len(rows_to_migrate)

        if verbose:
            print(f"  {table}: {len(rows_to_migrate)} rows")

    if not dry_run:
        dst_conn.commit()
    dst_conn.close()

    return total_rows, per_table, label_collisions


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate orphan Juggle DBs into canonical location."
    )
    parser.add_argument(
        "--src",
        required=True,
        help="Path to source (orphan) DB file"
    )
    parser.add_argument(
        "--dst",
        default="~/.claude/juggle/juggle.db",
        help="Path to destination (canonical) DB file (default: ~/.claude/juggle/juggle.db)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without writing"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show per-table row counts"
    )

    args = parser.parse_args()
    dst_path = Path(args.dst).expanduser()

    if args.dry_run:
        print(f"[DRY RUN] Would consolidate {args.src} into {dst_path}")

    total, per_table, collisions = consolidate(args.src, str(dst_path), args.dry_run, args.verbose)

    if total == 0:
        print(f"No new rows to migrate. (idempotent re-run)")
    else:
        print(f"Merged {total} rows across {len([t for t in per_table if per_table[t] > 0])} tables from {args.src} into {dst_path}")
        if collisions > 0:
            print(f"  Label collisions archived: {collisions}")


if __name__ == "__main__":
    main()
