"""juggle_vcs_validate — step 4 of ``/juggle:vcs-backend-init``: run the shared
conformance kit and assemble a fail-loud readiness report.

The conformance kit (vcs_conformance.py) is THE acceptance gate — a backend is
"correct" iff every kit test passes against it. ``run_conformance`` executes the
kit's ``test_*`` functions programmatically (no pytest at runtime) against a
duck-typed harness. The command NEVER reports ready on a red run: a
:class:`ReadinessReport` is ``ready`` only if every item is green.

Must not own: the harnesses (juggle_vcs_harness) or the kit itself
(vcs_conformance). It only drives them and scores the result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import vcs_conformance


@dataclass(frozen=True)
class ConformanceReport:
    """Per-test outcomes from one conformance run."""

    results: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for _n, ok, _e in self.results if ok)

    @property
    def all_green(self) -> bool:
        return self.total > 0 and self.passed == self.total

    def failures(self) -> list[tuple[str, str]]:
        return [(n, e) for n, ok, e in self.results if not ok]


def _kit_tests() -> list[tuple[str, object]]:
    """Every ``test_*`` function in the shared kit, in deterministic order."""
    return sorted(
        (n, f)
        for n, f in vars(vcs_conformance).items()
        if n.startswith("test_") and callable(f)
    )


def run_conformance(harness) -> ConformanceReport:
    """Run the shared conformance kit against ``harness`` and score each test.

    A test that raises is a FAILURE (recorded with its exception repr), never a
    crash of the runner — so one bad method can't hide the rest of the report."""
    results: list[tuple[str, bool, str]] = []
    for name, fn in _kit_tests():
        try:
            fn(harness)
            results.append((name, True, ""))
        except Exception as exc:  # a failing assertion or a raising method
            results.append((name, False, repr(exc)))
    return ConformanceReport(results=results)


@dataclass
class ReadinessItem:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class ReadinessReport:
    """The final go/no-go checklist. ``ready`` iff every item is green — the
    fail-loud gate the init command exits on."""

    items: list[ReadinessItem] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return bool(self.items) and all(i.ok for i in self.items)

    def render(self) -> list[str]:
        lines = []
        for i in self.items:
            mark = "✅" if i.ok else "❌"
            suffix = f" — {i.detail}" if i.detail else ""
            lines.append(f"  {mark} {i.label}{suffix}")
        return lines
