"""Self-heal recorder must honor the JUGGLE_DB_PATH redirect — never the frozen prod DB.

Regression: 2026-06-21 OSError in UserPromptSubmit from worktree.

Symptom: error_events 203-209,215 — dozens of OSError(ELOOP, errno 62) rows whose
tracebacks all originate in tests/test_hooks_fail_open.py (the fail-open suite that
deliberately raises ELOOP). Those test-simulated exceptions leaked into the REAL
~/.claude/juggle/juggle.db because juggle_selfheal._get_db() resolved the DB from
the import-frozen ``juggle_db.DB_PATH`` (always the prod path) instead of honoring
``JUGGLE_DB_PATH`` at call time the way every other DB open does (JuggleDB(None)
-> _resolve_db_path()). Under a worktree the frozen-prod open additionally trips
SharedDBMigrationRefused, so the recorder silently dropped the row; outside a
worktree it wrote straight into prod. Either way the recorder bypassed test
isolation.

Root cause is the lone bypass in _get_db(); the fix is to resolve the live path at
call time. These pins prove the recorder writes to the redirected DB and never to
the import-frozen prod path.
"""

import os
from pathlib import Path

from juggle_db import JuggleDB
from juggle_selfheal import _get_db, record_error

_ELOOP = OSError(62, "Too many levels of symbolic links")


def _raise_eloop():
    raise OSError(62, "Too many levels of symbolic links")


def test_get_db_honors_db_redirect_not_frozen_prod_path():
    """_get_db() must resolve JUGGLE_DB_PATH at call time, not the import-frozen prod DB_PATH.

    RED before fix: _get_db() opens the frozen prod DB_PATH and init_db() trips the
    prod/worktree guard (SharedDBMigrationRefused / TEST ISOLATION VIOLATION).
    """
    import juggle_db

    redirect = os.environ["JUGGLE_DB_PATH"]
    assert Path(juggle_db.DB_PATH) != Path(redirect), (
        "precondition: import-frozen DB_PATH must differ from the per-test redirect"
    )

    db = _get_db()
    assert str(db.db_path) == redirect, (
        "self-heal recorder must target the JUGGLE_DB_PATH redirect, not frozen prod DB_PATH"
    )


def test_record_error_from_userpromptsubmit_eloop_lands_in_redirected_db():
    """A UserPromptSubmit ELOOP recorded via self-heal must land in the redirected DB.

    Mirrors the worktree incident: the fail-open handler catches OSError(ELOOP) and
    calls record_error(..., "juggle_hooks.UserPromptSubmit"). The row must be written
    to the JUGGLE_DB_PATH-isolated DB — never lost to the prod guard, never to prod.

    RED before fix: _get_db() targets frozen prod -> guard raises -> record_error
    swallows it -> the redirected DB has 0 error_events.
    """
    os.environ.pop("JUGGLE_SELFHEAL_OP", None)

    try:
        _raise_eloop()
    except OSError as exc:
        record_error(exc, "juggle_hooks.UserPromptSubmit")

    redirect = os.environ["JUGGLE_DB_PATH"]
    db = JuggleDB(redirect)
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT exc_type, entrypoint FROM error_events"
        ).fetchall()

    assert len(rows) == 1, (
        f"recorder must write the ELOOP event to the redirected DB; got {len(rows)} rows "
        "(0 = recorder bypassed isolation and bounced off the prod guard)"
    )
    assert rows[0]["exc_type"] == "OSError"
    assert rows[0]["entrypoint"] == "juggle_hooks.UserPromptSubmit"


def test_record_error_never_touches_prod_db(monkeypatch):
    """Self-protection belt: even with JUGGLE_DB_PATH unset, record_error must not
    open the production DB (the conftest fail-closed guard would raise on a prod open;
    record_error swallows, so prod stays untouched and the redirected DB stays empty).

    This pins that the recorder's path resolution flows through the standard
    JuggleDB(None) seam, so a future regression that re-freezes the prod path is caught.
    """
    os.environ.pop("JUGGLE_SELFHEAL_OP", None)
    redirect = os.environ["JUGGLE_DB_PATH"]

    # With the redirect honored, the row lands in the isolated DB.
    try:
        _raise_eloop()
    except OSError as exc:
        record_error(exc, "juggle_hooks.UserPromptSubmit")

    db = JuggleDB(redirect)
    with db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM error_events").fetchone()["c"]
    assert count == 1
