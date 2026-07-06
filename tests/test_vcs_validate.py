"""tests/test_vcs_validate.py — vcs-backend-init step 4: the conformance kit is
THE acceptance gate. run_conformance drives the shared kit programmatically;
the readiness report is fail-loud (NEVER ready on a red conformance run).
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_vcs_harness import ToyHarness  # noqa: E402
from juggle_vcs_recipes import toy  # noqa: E402
from juggle_vcs_validate import (  # noqa: E402
    ReadinessItem,
    ReadinessReport,
    run_conformance,
)


def test_run_conformance_green_for_toy(tmp_path):
    report = run_conformance(ToyHarness(tmp_path))
    assert report.all_green is True
    assert report.passed == report.total
    assert report.total >= 13  # the full shared kit ran


def test_run_conformance_reports_red_on_broken_backend(tmp_path):
    class _BrokenHarness(ToyHarness):
        def __init__(self, tp):
            super().__init__(tp)
            self.vcs = _Broken()

    class _Broken(toy.ToyBackend):
        def is_ancestor(self, repo, rev, of):
            return False  # sabotage THE verified-gate primitive

    report = run_conformance(_BrokenHarness(tmp_path))
    assert report.all_green is False
    assert report.passed < report.total
    assert any(not ok for _n, ok, _e in report.results)


def test_readiness_not_ready_when_any_item_red():
    report = ReadinessReport(items=[
        ReadinessItem("backend loads", True),
        ReadinessItem("conformance green", False, "3 failed"),
    ])
    assert report.ready is False
    # the checklist renders a line per item, marked
    rendered = "\n".join(report.render())
    assert "conformance green" in rendered
    assert "3 failed" in rendered


def test_readiness_ready_when_all_green():
    report = ReadinessReport(items=[ReadinessItem("a", True), ReadinessItem("b", True)])
    assert report.ready is True
