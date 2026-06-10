"""Tests for juggle_cmd_graph — `juggle project-graph load` (autopilot Phase 1).

Covers: markdown spec parsing, validation (cycles, unknown/dup ids, empty
prompts, verify_cmd lint, node-count sanity), and guarded re-load semantics
(upsert by id; REFUSE changes to dispatching|running|integrating|verified).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402

SPEC = """# Demo graph

## schema: Add schema
Write the schema migration.

## api: Build API
deps: schema
verify_cmd: uv run pytest tests/test_api.py -q
Implement the API on top of the schema.

## ui: Build UI
deps: schema
Implement the UI.

## e2e: End-to-end
deps: api, ui
Wire it together.
"""


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _args(tmp_path, db, spec=SPEC, project="INBOX"):
    f = tmp_path / "graph.md"
    f.write_text(spec)
    return SimpleNamespace(file=str(f), project=project, db_path=str(db.db_path))


# ── cycle detection (pure, validation-owned) ───────────────────────────────────


def test_find_cycle_none_on_dag():
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    assert cg.find_cycle(["a", "b", "c", "d"], edges) is None


def test_find_cycle_detects_loop():
    edges = [("a", "b"), ("b", "c"), ("c", "a")]
    cyc = cg.find_cycle(["a", "b", "c"], edges)
    assert cyc is not None
    assert set(cyc) == {"a", "b", "c"}


def test_find_cycle_detects_self_loop():
    assert cg.find_cycle(["a"], [("a", "a")]) is not None


# ── parsing ────────────────────────────────────────────────────────────────────


def test_parse_graph_spec_basic():
    nodes = cg.parse_graph_spec(SPEC)
    assert [n["id"] for n in nodes] == ["schema", "api", "ui", "e2e"]
    api = nodes[1]
    assert api["title"] == "Build API"
    assert api["deps"] == ["schema"]
    assert api["verify_cmd"] == "uv run pytest tests/test_api.py -q"
    assert "Implement the API" in api["prompt"]
    assert nodes[0]["deps"] == []
    assert nodes[0]["verify_cmd"] is None
    assert nodes[3]["deps"] == ["api", "ui"]


def test_parse_accepts_bullet_field_lines():
    nodes = cg.parse_graph_spec("## a: A\n- deps: \n- verify_cmd: pytest -q\ndo a\n")
    assert nodes[0]["deps"] == []
    assert nodes[0]["verify_cmd"] == "pytest -q"
    assert nodes[0]["prompt"] == "do a"


# ── validation ─────────────────────────────────────────────────────────────────


def test_validate_clean_spec_no_errors():
    assert cg.validate_graph(cg.parse_graph_spec(SPEC)) == []


def test_validate_duplicate_ids():
    errs = cg.validate_graph(cg.parse_graph_spec("## a: A\nx\n## a: A2\ny\n"))
    assert any("duplicate" in e for e in errs)


def test_validate_unknown_dep():
    errs = cg.validate_graph(cg.parse_graph_spec("## a: A\ndeps: ghost\nx\n"))
    assert any("unknown" in e and "ghost" in e for e in errs)


def test_validate_empty_prompt():
    errs = cg.validate_graph(cg.parse_graph_spec("## a: A\n\n## b: B\nx\ndeps: a\n"))
    assert any("empty prompt" in e and "a" in e for e in errs)


def test_validate_cycle():
    spec = "## a: A\ndeps: b\nx\n## b: B\ndeps: a\ny\n"
    errs = cg.validate_graph(cg.parse_graph_spec(spec))
    assert any("cycle" in e for e in errs)


def test_validate_node_count_sanity():
    assert any("node count" in e for e in cg.validate_graph([]))
    many = "\n".join(f"## n{i}: N{i}\ndo {i}\n" for i in range(51))
    errs = cg.validate_graph(cg.parse_graph_spec(many))
    assert any("node count" in e for e in errs)


@pytest.mark.parametrize(
    "cmd",
    [
        "sh -c 'rm -rf /'",
        "bash test.sh",
        "pytest -q && rm x",
        "pytest -q > out.txt",
        "pytest -q | tee log",
        "pytest; rm x",
        "rm -rf build",
        "pytest `cmd`",
        "pytest $(cmd)",
    ],
)
def test_lint_verify_cmd_rejects(cmd):
    assert cg.lint_verify_cmd(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    ["pytest -q", "uv run pytest tests -q", "make check", "python3 scripts/verify.py"],
)
def test_lint_verify_cmd_accepts(cmd):
    assert cg.lint_verify_cmd(cmd) is None


# ── load command ───────────────────────────────────────────────────────────────


def test_load_creates_nodes_edges_and_ready_set(db, tmp_path, capsys):
    cg.cmd_project_graph_load(_args(tmp_path, db))
    nodes = {n["id"]: n for n in g.list_nodes(db, "INBOX")}
    assert set(nodes) == {"schema", "api", "ui", "e2e"}
    assert nodes["schema"]["state"] == "ready"  # root promoted on load
    assert nodes["api"]["state"] == "pending"
    assert sorted(g.get_deps(db, "e2e")) == ["api", "ui"]
    out = capsys.readouterr().out
    assert "4" in out and "ready" in out


def test_load_unknown_project_exits(db, tmp_path):
    with pytest.raises(SystemExit):
        cg.cmd_project_graph_load(_args(tmp_path, db, project="NOPE"))


def test_load_invalid_spec_exits_and_writes_nothing(db, tmp_path):
    bad = "## a: A\ndeps: ghost\nx\n"
    with pytest.raises(SystemExit):
        cg.cmd_project_graph_load(_args(tmp_path, db, spec=bad))
    assert g.list_nodes(db, "INBOX") == []


def test_reload_upserts_unprotected_nodes(db, tmp_path):
    cg.cmd_project_graph_load(_args(tmp_path, db))
    updated = SPEC.replace("Implement the UI.", "Implement the UI v2.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=updated))
    assert g.get_node(db, "ui")["prompt"] == "Implement the UI v2."


def test_reload_refuses_change_to_protected_node(db, tmp_path):
    """Re-load is REFUSED for nodes in dispatching|running|integrating|verified."""
    cg.cmd_project_graph_load(_args(tmp_path, db))
    for ev in ("claim", "dispatch"):  # schema: ready → running
        g.node_transition(db, "schema", ev)
    updated = SPEC.replace("Write the schema migration.", "CHANGED prompt.")
    with pytest.raises(SystemExit):
        cg.cmd_project_graph_load(_args(tmp_path, db, spec=updated))
    # nothing written, including other nodes (atomic refusal)
    assert g.get_node(db, "schema")["prompt"] == "Write the schema migration."
    assert g.get_node(db, "schema")["state"] == "running"


def test_reload_unchanged_protected_node_is_ok(db, tmp_path):
    cg.cmd_project_graph_load(_args(tmp_path, db))
    for ev in ("claim", "dispatch"):
        g.node_transition(db, "schema", ev)
    updated = SPEC.replace("Implement the UI.", "Implement the UI v2.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=updated))  # no raise
    assert g.get_node(db, "ui")["prompt"] == "Implement the UI v2."
    assert g.get_node(db, "schema")["state"] == "running"


def test_reload_resets_failed_node_to_pending(db, tmp_path):
    cg.cmd_project_graph_load(_args(tmp_path, db))
    for ev in ("claim", "dispatch", "exec_fail"):
        g.node_transition(db, "schema", ev)
    updated = SPEC.replace("Write the schema migration.", "Fixed prompt.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=updated))
    node = g.get_node(db, "schema")
    assert node["prompt"] == "Fixed prompt."
    # failed node re-enters the pipeline: reload → pending → ready (no deps)
    assert node["state"] == "ready"


# ── BLOCKER-1 (DA round-2, 2026-06-10): blocked-failed resume via reload ──────


def _walk(db, node_id, *events):
    for ev in events:
        g.node_transition(db, node_id, ev)


def test_reload_resumes_blocked_tail_end_to_end(db, tmp_path):
    """REGRESSION PIN (DA round-2 BLOCKER-1, 2026-06-10): a mid-diamond failure
    blocked the tail (blocked-failed) but _TRANSITIONS had no way out of
    blocked-failed — reloading the fixed spec resurrected the failed node while
    its dependents stayed dead forever (and a reload that also edited the
    blocked node crashed with an uncaught ValueError mid-upsert).

    Full resume path: fail mid-diamond → reload edited spec → blocked tail
    resumes → whole diamond verifies."""
    cg.cmd_project_graph_load(_args(tmp_path, db))
    # schema verifies; api + ui become ready
    _walk(db, "schema", "claim", "dispatch", "integrate_start", "integrate_ok")
    assert g.recompute_ready(db, "INBOX") == ["api", "ui"]
    # api fails mid-diamond → e2e blocked-failed; ui still verifies
    _walk(db, "api", "claim", "dispatch", "integrate_start", "integrate_fail")
    g.propagate_failure(db, "api")
    assert g.get_node(db, "e2e")["state"] == "blocked-failed"
    _walk(db, "ui", "claim", "dispatch", "integrate_start", "integrate_ok")

    # operator fixes the spec: api AND the (still-blocked) e2e prompt edited
    fixed = SPEC.replace(
        "Implement the API on top of the schema.", "Implement the API v2."
    ).replace("Wire it together.", "Wire it together v2.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=fixed))

    api = g.get_node(db, "api")
    e2e = g.get_node(db, "e2e")
    assert api["prompt"] == "Implement the API v2."
    assert api["state"] == "ready"  # failed → reload → pending → ready
    # the blocked tail resumed: no remaining dep is failed-*/blocked-failed
    assert e2e["state"] == "pending"
    assert e2e["prompt"] == "Wire it together v2."

    # diamond completes
    _walk(db, "api", "claim", "dispatch", "integrate_start", "integrate_ok")
    assert g.recompute_ready(db, "INBOX") == ["e2e"]
    _walk(db, "e2e", "claim", "dispatch", "integrate_start", "integrate_ok")
    assert g.get_node(db, "e2e")["state"] == "verified"


def test_reload_keeps_tail_blocked_while_any_dep_still_failed(db, tmp_path):
    """REGRESSION PIN (DA round-2 BLOCKER-1 closure semantics, 2026-06-10):
    a blocked-failed node returns to pending IFF no remaining dep is in a
    failed-*/blocked-failed state — fixing one of two failed deps must NOT
    resume the tail."""
    cg.cmd_project_graph_load(_args(tmp_path, db))
    _walk(db, "schema", "claim", "dispatch", "integrate_start", "integrate_ok")
    g.recompute_ready(db, "INBOX")
    _walk(db, "api", "claim", "dispatch", "integrate_start", "integrate_fail")
    g.propagate_failure(db, "api")
    _walk(db, "ui", "claim", "dispatch", "exec_fail")
    g.propagate_failure(db, "ui")
    assert g.get_node(db, "e2e")["state"] == "blocked-failed"

    # fix ONLY api — ui is still failed-exec, so e2e must stay blocked
    fix_api = SPEC.replace("Implement the API on top of the schema.", "API v2.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=fix_api))
    assert g.get_node(db, "api")["state"] == "ready"
    assert g.get_node(db, "ui")["state"] == "failed-exec"
    assert g.get_node(db, "e2e")["state"] == "blocked-failed"

    # now fix ui too → tail resumes
    fix_both = fix_api.replace("Implement the UI.", "UI v2.")
    cg.cmd_project_graph_load(_args(tmp_path, db, spec=fix_both))
    assert g.get_node(db, "ui")["state"] == "ready"
    assert g.get_node(db, "e2e")["state"] == "pending"


def test_load_upserts_are_atomic_all_or_nothing(db, tmp_path, monkeypatch):
    """REGRESSION PIN (DA round-2 BLOCKER-1c, 2026-06-10): per-node commits in
    cmd_project_graph_load meant a mid-loop crash (e.g. the pre-fix uncaught
    ValueError on reloading an edited blocked node) left EARLIER nodes already
    upserted — a half-applied spec. The load must be one transaction: any
    failure rolls back every upsert and exits non-zero."""
    cg.cmd_project_graph_load(_args(tmp_path, db))

    calls = {"n": 0}
    real = cg.db_graph.update_node_content

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("simulated crash mid-load")
        return real(*a, **kw)

    monkeypatch.setattr(cg.db_graph, "update_node_content", boom)
    edited = SPEC.replace(
        "Implement the API on top of the schema.", "API v2."
    ).replace("Implement the UI.", "UI v2.")
    with pytest.raises(SystemExit):
        cg.cmd_project_graph_load(_args(tmp_path, db, spec=edited))

    # NOTHING changed — including the node whose update "succeeded" pre-crash
    assert g.get_node(db, "api")["prompt"] == "Implement the API on top of the schema."
    assert g.get_node(db, "ui")["prompt"] == "Implement the UI."


# ── MAJOR-2 (DA round-2, 2026-06-10): PR-mode repos unsupported ────────────────


def test_load_refuses_pr_push_mode_repo(db, tmp_path, monkeypatch, capsys):
    """REGRESSION PIN (DA round-2 MAJOR-2, 2026-06-10): on push_mode='pr'
    repos _run_integrate returns success after only pushing the branch — the
    node went 'verified' WITHOUT any merge, and dependents were hydrated with
    'already integrated into main' (false). Policy: refuse project-graph load
    for PR-mode repos until autopilot supports them."""
    import juggle_settings

    monkeypatch.setattr(cg, "_git_root", lambda cwd: "/fake/pr-repo")
    monkeypatch.setattr(
        juggle_settings,
        "get_repo_config",
        lambda p: {"push_mode": "pr", "test_cmd": ""},
    )
    with pytest.raises(SystemExit):
        cg.cmd_project_graph_load(_args(tmp_path, db))
    assert g.list_nodes(db, "INBOX") == []  # nothing written
    err = capsys.readouterr().err
    assert "push_mode='pr'" in err and "not supported" in err


def test_load_allows_direct_and_none_push_modes(db, tmp_path, monkeypatch):
    import juggle_settings

    monkeypatch.setattr(cg, "_git_root", lambda cwd: "/fake/repo")
    monkeypatch.setattr(
        juggle_settings,
        "get_repo_config",
        lambda p: {"push_mode": "none", "test_cmd": ""},
    )
    cg.cmd_project_graph_load(_args(tmp_path, db))  # no raise
    assert len(g.list_nodes(db, "INBOX")) == 4
