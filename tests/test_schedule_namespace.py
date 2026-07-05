"""P0b schedule-namespace consolidation pins (2026-07-04 loop-entity V2).

Incident anchor: the bare `/juggle:schedule` command duplicated the launchd/
systemd/cron backend prose that `/juggle:schedule:create` also needs. V2
consolidates onto ONE `schedule:{create,list,delete}` namespace, homes the
backend how-to in a shared reference doc OUTSIDE ``commands/`` (recursive command
discovery would otherwise register a doc under ``commands/`` as a phantom slash
command), and adds a type-dispatched ``:delete`` (soft loop pause+project-close
vs OS ``uninstall``; ``--purge`` = hard loop removal).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
COMMANDS = REPO / "commands"
DOCS = REPO / "docs"


# ── :create no longer references the deleted bare command ─────────────────────
def test_create_no_longer_refs_bare():
    """create-no-longer-refs-bare (2026-07-04): schedule:create must not point at
    the deleted bare `/juggle:schedule`; the bare command file must be gone; and
    the shared OS-backend reference must exist OUTSIDE commands/."""
    create = (COMMANDS / "schedule:create.md").read_text()
    # A namespaced ref (`/juggle:schedule:create`) is fine; a BARE `/juggle:schedule`
    # (the deleted command) is not — bare == `/juggle:schedule` NOT followed by `:`.
    assert not re.search(r"/juggle:schedule(?!:)", create), (
        "schedule:create.md still references the deleted bare /juggle:schedule"
    )
    assert not (COMMANDS / "schedule.md").exists(), (
        "the bare commands/schedule.md must be deleted"
    )
    ref = DOCS / "schedule-os-backends.md"
    assert ref.exists(), "shared OS-backend reference doc must exist (outside commands/)"
    assert "schedule-os-backends" in create, (
        "schedule:create.md must point at the shared backend reference"
    )
    # The shared reference lives OUTSIDE commands/ so recursive command discovery
    # can't register it as a phantom slash command.
    assert ref.parent != COMMANDS


# ── fakes for the OS-scheduler backend seam ───────────────────────────────────
class _FakeTask:
    def __init__(self, label, schedule="every 15m", status="ok", pid=None, log_path=None):
        self.label = label
        self.schedule = schedule
        self.status = status
        self.pid = pid
        self.log_path = log_path


class _FakeBackend:
    def __init__(self, tasks=None):
        self._tasks = list(tasks or [])
        self.uninstalled: list[str] = []

    def list_tasks(self):
        return list(self._tasks)

    def uninstall(self, label):
        self.uninstalled.append(label)


# ── :list surfaces OS schedules + loops, each type-tagged ─────────────────────
def test_list_type_tagged(juggle_db):
    """list-type-tagged (2026-07-04): :list surfaces BOTH OS schedules (list_tasks)
    and loops (list_active_loops), each row tagged with its type."""
    from juggle_cmd_schedule import list_schedules

    pid = juggle_db.create_project(name="loopP", objective="o")
    loop_id = juggle_db.create_loop(project_id=pid, cadence="daily at 08:00")
    backend = _FakeBackend([_FakeTask("news-ingest")])

    rows = list_schedules(juggle_db, backend=backend)
    by_type: dict[str, list] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)

    assert "os" in by_type and "loop" in by_type, f"missing a type tag: {rows}"
    assert any(r["id"] == "news-ingest" for r in by_type["os"])
    assert any(r["id"] == loop_id for r in by_type["loop"])


# ── :delete is soft + type-dispatched; --purge hard-removes a loop ────────────
def test_delete_type_dispatched(juggle_db):
    """delete-type-dispatched (2026-07-04): loop delete is SOFT by default (pause +
    project-close, recoverable), OS delete calls uninstall(label), and --purge
    hard-removes the loop row."""
    from juggle_cmd_schedule import delete_schedule

    # LOOP soft: pause + project-close, row PRESERVED (recoverable)
    pid = juggle_db.create_project(name="loopP", objective="o")
    loop_id = juggle_db.create_loop(project_id=pid, cadence="daily")
    backend = _FakeBackend([_FakeTask("news-ingest")])

    res = delete_schedule(juggle_db, loop_id, backend=backend)
    assert res["type"] == "loop" and res["action"] == "soft"
    assert juggle_db.get_loop(loop_id)["status"] == "paused"   # paused, not gone
    assert juggle_db.get_project(pid)["status"] == "closed"    # project closed
    assert backend.uninstalled == []                           # never touched OS backend

    # LOOP purge: hard row removal (non-recoverable)
    pid2 = juggle_db.create_project(name="loopP2", objective="o")
    loop2 = juggle_db.create_loop(project_id=pid2, cadence="daily")
    res2 = delete_schedule(juggle_db, loop2, purge=True, backend=backend)
    assert res2["type"] == "loop" and res2["action"] == "purge"
    assert juggle_db.get_loop(loop2) is None                   # row gone

    # OS: type-dispatch to uninstall(label)
    res3 = delete_schedule(juggle_db, "news-ingest", backend=backend)
    assert res3["type"] == "os" and res3["action"] == "uninstall"
    assert backend.uninstalled == ["news-ingest"]


def test_unknown_id_fails_loud(juggle_db):
    """unknown-id-fails-loud (2026-07-04): an id that is neither a loop nor a known
    OS schedule must raise (fail loud), never silently no-op."""
    from juggle_cmd_schedule import delete_schedule

    backend = _FakeBackend([])  # no OS tasks, no matching loop
    with pytest.raises(Exception):
        delete_schedule(juggle_db, "does-not-exist", backend=backend)


def test_delete_backend_enumeration_error_surfaces(juggle_db):
    """delete-backend-error-not-masked (2026-07-04 code-review Important-1): when the
    OS backend fails to ENUMERATE during a delete, the real error must surface — not
    be swallowed and masqueraded as a misleading 'unknown id' no-op
    (detect-refuse-preserve). RED pre-fix: _resolve_os caught list_tasks() and
    returned [], so delete raised ValueError('unknown id') instead of the backend
    error."""
    from juggle_cmd_schedule import delete_schedule

    class _BoomBackend:
        def list_tasks(self):
            raise RuntimeError("launchctl unavailable")

        def uninstall(self, label):  # pragma: no cover - must never be reached
            raise AssertionError("must not uninstall when enumeration failed")

    with pytest.raises(RuntimeError, match="launchctl unavailable"):
        delete_schedule(juggle_db, "some-os-label", backend=_BoomBackend())
