#!/usr/bin/env python3
"""Juggle — migration command: reserve the next DB-schema migration number.

P2 (2026-07-03, irl-prevention): `juggle migration next` replaces eyeballing
`dbops/migration_*.py` for the highest number — the source of the 2026-07-02
duplicate-column incident when two coders picked the same number concurrently.
"""


def get_db(db_path=None):
    from juggle_cli_common import get_db as _get_db
    return _get_db(db_path)


def cmd_migration_next(args):
    """`juggle migration next` — reserve and print the next migration number."""
    from dbops.migration_seq import reserve_next

    db = get_db(getattr(args, "db_path", None))
    conn = db._connect()
    n = reserve_next(conn)
    print(n)
    return n
