"""Shared gate logic for the P8-completion watchdog verify-cmds.

Each ``scripts/p8_verify_step<N>.py`` is a thin wrapper that calls ``run(N)``.
The watchdog runs ``uv run python scripts/p8_verify_step<N>.py`` as a task
node's done-gate (``uv`` is on the verify_cmd allowlist; a bare ``sh``/script
path is NOT — see ``juggle_graph_upsert.VERIFY_CMD_ALLOWLIST``).

Gate contract per step (from ``plan/2026-06-27-p8-completion.md`` "Step-N DONE
when" lines): the FULL pytest suite is green AND the step's absolute,
self-contained monotonic source counter has reached its floor. Cheap source
scans run FIRST (fail-fast, no suite cost) so an un-started step exits 1 quickly.
Checks that depend on future test names or the ``doctor --pre-p8-check`` flag are
delegated to suite-green (the step's TDD pins live in the suite), keeping these
helpers dependency-free (no DB, no migration guard, cwd-independent).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _iter_py(root: Path):
    if root.is_file():
        yield root
        return
    for p in sorted(root.rglob("*.py")):
        yield p


def grep_count(pattern: str, *, paths=("src",), exclude_names=()) -> int:
    """Count source LINES matching ``pattern`` (regex) under ``paths``."""
    rx = re.compile(pattern)
    n = 0
    for rel in paths:
        root = REPO / rel
        if not root.exists():
            continue
        for f in _iter_py(root):
            if f.name in exclude_names:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n += sum(1 for line in text.splitlines() if rx.search(line))
    return n


def file_absent(rel: str) -> bool:
    return not (REPO / rel).exists()


def imports_ok(modules) -> bool:
    """True iff ``import <modules>`` succeeds with src on the path."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    code = "import " + ", ".join(modules)
    r = subprocess.run(
        ["uv", "run", "python", "-c", code], cwd=str(REPO), env=env,
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  import FAILED: {r.stderr.strip().splitlines()[-1:] or r.stderr}")
    return r.returncode == 0


def suite_green() -> bool:
    """Run the FULL pytest suite once; True iff it exits 0."""
    env = dict(os.environ)
    env.setdefault("CLAUDE_PLUGIN_DATA", str(Path.home() / ".claude" / "juggle"))
    env.setdefault("JUGGLE_MAX_BACKGROUND_AGENTS", "5")
    env.setdefault("JUGGLE_MAX_THREADS", "10")
    r = subprocess.run(
        ["uv", "run", "pytest", "-q"], cwd=str(REPO), env=env,
        capture_output=True, text=True,
    )
    tail = "\n".join((r.stdout + r.stderr).splitlines()[-3:])
    print(f"  suite: {'PASS' if r.returncode == 0 else 'FAIL'}\n{tail}")
    return r.returncode == 0


def run(step: int) -> None:
    """Evaluate step ``step``: source gates first, then suite. Exit 0/1."""
    gate = _GATES.get(step)
    if gate is None:
        print(f"unknown step {step}")
        sys.exit(2)
    print(f"[p8-verify step {step}] {gate.__doc__ or ''}".strip())
    failures = gate()
    if failures:
        for f in failures:
            print(f"  GATE FAIL: {f}")
        print(f"[p8-verify step {step}] FAIL (source gate)")
        sys.exit(1)
    print("  source gates: PASS")
    if os.environ.get("P8_VERIFY_SKIP_SUITE") == "1":
        print("  (suite skipped: P8_VERIFY_SKIP_SUITE=1)")
        sys.exit(0)
    sys.exit(0 if suite_green() else 1)


# --- per-step source gates: return a list of human-readable failures (empty=ok)

_MIGRATION_EXCLUDE = {"migrations_nodes.py", "migration_51_state_vocab.py"}


def _step1():
    """one vocab 'open' + one transition engine"""
    fails = []
    pend = grep_count(r"'pending'", exclude_names=_MIGRATION_EXCLUDE)
    if pend:
        fails.append(f"'pending' still in live code: {pend} line(s)")
    calls = grep_count(r"node_transition\(") - grep_count(r"def node_transition\(")
    if calls <= 0:
        fails.append("no node_transition() call sites found")
    return fails


def _step2():
    """centralize vocab maps + background first-class"""
    fails = []
    fwd = grep_count(r'"active": "open"')
    if fwd != 1:
        fails.append(f'forward-map "active": "open" count={fwd} (want 1)')
    return fails


def _step3():
    """conversation cluster flip + delete shim"""
    fails = []
    if grep_count(r"CONV_ALIAS_SHIM"):
        fails.append("CONV_ALIAS_SHIM not deleted")
    bg = grep_count(
        r"status\s*==\s*['\"]background['\"]|status=['\"]background['\"]"
        r"|FROM threads WHERE status='background'"
    )
    if bg:
        fails.append(f"legacy background status refs remain: {bg}")
    mods = ["dbops.threads", "juggle_watchdog", "juggle_dispatch_core",
            "juggle_cmd_context", "juggle_context_startup"]
    if not imports_ok(mods):
        fails.append("conversation-cluster modules not importable")
    return fails


def _step4():
    """graph cluster flip; delete legacy writes/reads"""
    fails = []
    ins = grep_count(
        r"INSERT INTO graph_tasks|INSERT INTO graph_edges|INSERT OR IGNORE INTO graph_",
        paths=("src/juggle_add_node.py",),
    )
    if ins:
        fails.append(f"legacy graph_* writes remain in add_node: {ins}")
    reads = grep_count(
        r"FROM threads|FROM graph_topics|FROM graph_tasks",
        paths=("src/juggle_cockpit_model.py", "src/dbops/orphan_guard.py"),
    )
    if reads:
        fails.append(f"legacy reads remain in cockpit/orphan_guard: {reads}")
    mir = grep_count(r"db_mirror")
    if mir:
        fails.append(f"db_mirror refs remain: {mir}")
    return fails


def _step5():
    """honest DDL + honest Gate-A"""
    fails = []
    if grep_count(r"excluded_files", paths=("src/dbops/p8_readiness.py",)) <= 0:
        fails.append("Gate-A does not report excluded_files")
    if grep_count(r"import_refs", paths=("src/dbops/p8_readiness.py",)) <= 0:
        fails.append("Gate-A does not report import_refs")
    return fails


def _step6():
    """post-collapse cleanup + terminal drop"""
    fails = []
    if not file_absent("src/juggle_migrate_lifecycle.py"):
        fails.append("juggle_migrate_lifecycle.py not deleted")
    if grep_count(r"dispatch_thread_id"):
        fails.append("dispatch_thread_id still referenced in src")
    if grep_count(r"db_mirror"):
        fails.append("db_mirror still referenced in src")
    pend = grep_count(r"'pending'", exclude_names=_MIGRATION_EXCLUDE)
    if pend:
        fails.append(f"'pending' still in live code: {pend}")
    return fails


_GATES = {1: _step1, 2: _step2, 3: _step3, 4: _step4, 5: _step5, 6: _step6}
