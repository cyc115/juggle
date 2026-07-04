"""Phase3 extraction pin: the graph *mutator* command handlers live in
juggle_cmd_graph_ops (headroom under the 300-LOC gate for new graph commands),
while juggle_cmd_graph re-exports them so every existing
``from juggle_cmd_graph import cmd_graph_*`` / ``cg.cmd_graph_*`` caller and the
test-suite monkeypatches keep working unchanged.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_cmd_graph as cg  # noqa: E402
import juggle_cmd_graph_ops as ops  # noqa: E402

_HANDLERS = ("cmd_graph_add_task", "cmd_graph_reconcile", "cmd_graph_mark_task")


def test_handlers_defined_in_ops_module():
    for name in _HANDLERS:
        fn = getattr(ops, name)
        assert fn.__module__ == "juggle_cmd_graph_ops", name


def test_juggle_cmd_graph_reexports_same_objects():
    for name in _HANDLERS:
        assert getattr(cg, name) is getattr(ops, name), name


def test_moved_handlers_resolve_get_db_via_cg_namespace(monkeypatch):
    """The atomicity/spool tests monkeypatch ``cg.get_db``; the extracted
    handlers must look ``get_db`` up through the juggle_cmd_graph namespace so
    the patch still takes effect after the move."""
    seen = {}

    def _fake_get_db(db_path, init=False):
        seen["called"] = True
        raise SystemExit(0)  # short-circuit before touching a real DB

    monkeypatch.setattr(cg, "get_db", _fake_get_db)
    from types import SimpleNamespace

    try:
        cg.cmd_graph_reconcile(
            SimpleNamespace(project="X", db_path=None, json_out=False)
        )
    except SystemExit:
        pass
    assert seen.get("called") is True
