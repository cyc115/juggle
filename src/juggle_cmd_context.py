#!/usr/bin/env python3
"""Juggle CLI — Shared context, memory, domain, and misc commands."""

import json
import subprocess
import sys

from juggle_cli_common import (
    SRC_DIR,
    DB_PATH,
    _get_hindsight_client,
    _resolve_thread,
    get_db,
)
from juggle_db import DEFAULT_DATA_DIR as _DATA_DIR


def cmd_get_shared_context(args):
    db = get_db()
    rows = db.get_shared_context()

    if args.type:
        rows = [r for r in rows if r["context_type"] == args.type]
    if args.thread:
        rows = [r for r in rows if r.get("source_thread") == args.thread]
    if args.limit:
        rows = rows[-args.limit:]

    if args.plain:
        if not rows:
            print("(no shared context)")
            return
        for r in rows:
            src = f" (Thread {r['source_thread']})" if r.get("source_thread") else ""
            print(f"[{r['context_type']}]{src} {r['content']}")
    else:
        print(json.dumps(rows, indent=2))


def cmd_add_shared(args):
    db = get_db()
    db.add_shared(args.type, args.content, source_thread=args.thread)
    print(f"Added [{args.type}]: {args.content}")


def cmd_get_context(_):
    sys.path.insert(0, str(SRC_DIR))
    from juggle_context import build_context_string
    result = build_context_string(db_path=str(DB_PATH))
    print(result)


def cmd_init_db(_):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.init_db()
    print("DB initialized.")


def cmd_recall(args):
    """Recall memories from Hindsight for a thread."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    result = client.reflect(args.query)

    if result:
        db.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
        print(result)
    else:
        db.update_thread(thread_uuid, memory_loaded=1)


def cmd_recall_if_cold(args):
    """Recall only if thread hasn't loaded memory yet."""
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)
    if thread.get("memory_loaded", 0):
        return  # already loaded, no-op

    client = _get_hindsight_client()
    if client is None:
        return

    result = client.reflect(args.query)
    if result:
        db.update_thread(thread_uuid, memory_context=result, memory_loaded=1)
        print(result)
    else:
        db.update_thread(thread_uuid, memory_loaded=1)


def cmd_retain(args):
    """Retain content as memory in Hindsight."""
    client = _get_hindsight_client()
    if client is None:
        return  # disabled or unconfigured

    context = getattr(args, "context", None)
    client.retain(args.content, context=context)


def cmd_grep_vault(args):
    """Search vault for terms. Returns matching file paths only."""
    vault = args.vault_path
    results = []
    for term in args.terms[:5]:
        try:
            proc = subprocess.run(
                ["grep", "-ril", "--include=*.md", term, vault],
                capture_output=True, text=True, timeout=5,
            )
            for line in proc.stdout.strip().split("\n"):
                if line and line not in results:
                    results.append(line)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    if results:
        print("\n".join(results[:20]))


def cmd_register_domain(args):
    db = get_db()
    db.register_domain(args.name)
    print(f"Domain '{args.name}' registered.")


def cmd_register_domain_path(args):
    db = get_db()
    if not db.is_known_domain(args.domain):
        print(f"Unknown domain '{args.domain}'. Run: juggle register-domain {args.domain}")
        sys.exit(1)
    db.add_domain_path(args.path_fragment, args.domain)
    print(f"Path '{args.path_fragment}' → domain '{args.domain}' registered.")
