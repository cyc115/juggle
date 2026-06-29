"""As-built verification for ``specs/2026-06-18-unified-topic-graph.md`` (P8 H5 + R3-3).

2026-06-29 P8 c6-spec — symptom: the unified-topic-graph spec still claimed
"LOCKED design" long after P8 collapsed the data model + state machine, so the
spec drifted from the shipped code. These pins demote the spec to as-built and
lock the invariants the earlier P8 nodes established:

  (a) the legacy task-entry state value ``'pending'`` is gone from LIVE ``src/``
      (the single task vocab is ``'open'``). The two dated migrations that carry
      the historical value by design (M44/M51 double value-migration) are excluded.
  (b) no STEADY-STATE query reads the raw ``nodes.dispatch_thread_id`` column — the
      task->dispatch-thread binding is a typed ``kind='dispatch'`` ``node_edge``.
      The raw column legitimately persists in the schema (``schema_nodes.py``) +
      the ``migration_*`` files until the terminal drop rebuild, so those are
      excluded. This pin only proves no live query still READS the column.
  (c) the spec no longer carries a bare "LOCKED design" header without an as-built
      / SUPERSEDED note.
  (d) the §10 deletion-list items that the P8 collapse ACTUALLY eliminated are
      gone from ``src/`` — the cockpit ``task_state_by_thread`` JOIN (reads come
      straight from ``nodes`` now) and the ``db_mirror`` engine module.

  NOTE on (d) scope — §10 is the FULL-project (P1–P8) deletion target and is only
  PARTIALLY realized as-built; this pin asserts ONLY the genuinely-removed items.
  The BULK of §10 is intentionally NOT asserted because it is not gone yet:
    * the legacy-TABLE rows (``CREATE_GRAPH_*``, the ``threads`` DDL,
      ``set_topic_thread`` / ``graph_topics.is_mirror``) are removed only by the
      terminal drop migration (plan Task 6.3), gated + deferred (OQ1 soak);
    * the CLI-surface rows (``get-agent`` / ``send-task`` / ``release-agent``
      registration, per-project arming + ``autopilot_armed_project``,
      ``--force-task`` / ``check_task_guard``, ``_dispatch_flat_task_fallback``,
      ``_dispatch_via_pool``) were kept as COMPAT shims and are still live;
    * ``reconcile_out_of_band_merges`` is explicitly RETAINED by §10.1 (renamed
      ``verify_merged_nodes``), so it was never "eliminated".
  The as-built addendum scopes all three honestly — see the spec.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SRC = _REPO / "src"
_SPEC = _REPO / "specs" / "2026-06-18-unified-topic-graph.md"


def _py_files() -> list[Path]:
    return sorted(_SRC.rglob("*.py"))


def test_pending_absent_from_live_src() -> None:
    """(a) P8 C3: ``'pending'`` is dead vocab; the single task-entry state is
    ``'open'``. Excludes the dated migrations ``migrations_nodes.py`` +
    ``migration_51_state_vocab.py``, which carry the historical value by design."""
    exclude = {"migrations_nodes.py", "migration_51_state_vocab.py"}
    pat = re.compile(r"""['"]pending['"]""")
    offenders = []
    for f in _py_files():
        if f.name in exclude:
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if pat.search(line):
                offenders.append(f"{f.relative_to(_REPO)}:{i}: {line.strip()}")
    assert not offenders, "live 'pending' literal still present:\n" + "\n".join(offenders)


def test_dispatch_thread_id_absent_from_steady_state_queries() -> None:
    """(b) P8 M1/Q2: the task->dispatch-thread binding is a typed
    ``kind='dispatch'`` ``node_edge``; no live query reads the raw
    ``nodes.dispatch_thread_id`` column. The raw column persists in the schema
    (``schema_nodes.py``) + the ``migration_*`` files until the terminal drop
    rebuild, so those are excluded."""
    offenders = []
    for f in _py_files():
        if f.name.startswith("migration_") or f.name == "schema_nodes.py":
            continue
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "dispatch_thread_id" in line:
                offenders.append(f"{f.relative_to(_REPO)}:{i}: {line.strip()}")
    assert not offenders, "live dispatch_thread_id read still present:\n" + "\n".join(offenders)


def test_spec_demoted_no_bare_locked() -> None:
    """(c) P8 H5: the spec is demoted to as-built — no bare 'LOCKED design' header,
    no line starting with 'LOCKED', and the SUPERSEDED / as-built marker present."""
    text = _SPEC.read_text()
    assert "LOCKED design" not in text, "spec still carries a bare 'LOCKED design' header"
    assert not any(line.startswith("LOCKED") for line in text.splitlines()), \
        "spec has a line starting with 'LOCKED'"
    assert "SUPERSEDED" in text and "as-built" in text.lower(), \
        "spec must mark itself SUPERSEDED with an as-built addendum"


# §10 deletion-list items the P8 collapse ACTUALLY eliminated (each verified gone).
# This is a deliberate SUBSET of §10 — the rest is compat-retained or deferred to
# the terminal drop; see the module docstring, NOTE on (d) scope.
_SECTION_10_ELIMINATED = (
    "task_state_by_thread",  # §10: cockpit-model thread JOIN -> direct nodes.state read
)


def test_section10_eliminated_items_absent_from_src() -> None:
    """(d) P8 H5: the §10 deletion-list items P8 actually removed are gone from
    ``src/`` — the cockpit ``task_state_by_thread`` JOIN and the ``db_mirror``
    engine module. The remaining §10 rows are compat-retained or deferred to the
    terminal drop (see the module docstring + the spec's as-built addendum)."""
    blob = {f.relative_to(_REPO): f.read_text() for f in _py_files()}
    for sym in _SECTION_10_ELIMINATED:
        hits = [str(p) for p, txt in blob.items() if sym in txt]
        assert not hits, f"§10-eliminated {sym!r} still present in: {hits}"
    # The db_mirror engine module (§10 / Step-4 "mirror concept dead") is deleted.
    assert not (_SRC / "dbops" / "db_mirror.py").exists(), \
        "db_mirror.py must be gone (the mirror concept is dead)"
