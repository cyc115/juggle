"""Hermetic + livelock-proof pins for the cockpit --smoke body-capture loop.

Defect C: ``capture_body_frame`` re-polls ``handle.frame()`` while the grid is
mostly blank, bounded ONLY by a wall-clock deadline. When ``frame()`` returns
instantly — a dead cockpit child hitting EOF, or a test fake — the loop becomes
a busy-spin (livelock) that burns 100% CPU for the full deadline AND makes the
harness non-hermetic (its iteration count is a function of real wall-clock time,
not the inputs).

These tests pin the fix WITHOUT a PTY: the re-poll loop must be bounded by a
hard poll cap, so it terminates deterministically regardless of ``max_wait``.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("yaml", reason="pyyaml not installed")


class _CountingBlankHandle:
    """Fake handle whose frame() returns a mostly-blank grid INSTANTLY.

    Models a dead cockpit child (EOF → frame() returns at once). Raises after
    `runaway` calls so a livelocking loop surfaces as a hard failure instead of
    an effectively-infinite spin.
    """

    def __init__(self, cols: int = 80, rows: int = 67, runaway: int = 5000,
                 body_at_call: int | None = None):
        self.cols = cols
        self.rows = rows
        self.runaway = runaway
        self.body_at_call = body_at_call
        self.frame_calls = 0

    def frame(self, settle: float = 1.0, timeout: float = 10.0) -> list[str]:
        self.frame_calls += 1
        if self.frame_calls > self.runaway:
            raise RuntimeError(
                f"livelock: frame() called {self.frame_calls}× — re-poll loop "
                "is not bounded by a poll cap"
            )
        painted = (
            self.body_at_call is not None and self.frame_calls >= self.body_at_call
        )
        grid = [" " * self.cols for _ in range(self.rows)]
        grid[0] = "Juggle Cockpit".ljust(self.cols)
        grid[-1] = "q Quit".ljust(self.cols)
        if painted:
            for i in range(1, self.rows - 1):
                grid[i] = f"row{i:03d} body content".ljust(self.cols)
        return grid


def test_capture_body_frame_bounded_when_body_never_paints():
    """Livelock guard: a body that never paints + instant frame() must NOT spin.

    With a huge ``max_wait`` the wall-clock deadline cannot be what stops the
    loop, so a correct implementation must stop on a hard poll cap. A livelocking
    implementation blows past `runaway` and raises.
    """
    from juggle_smoke import capture_body_frame

    handle = _CountingBlankHandle(runaway=5000)
    grid = capture_body_frame(handle, rows=handle.rows, max_wait=3600.0)

    # Returned the last (blank) grid so real_estate still fails downstream.
    assert grid, "expected the last captured grid, blank or not"
    # Bounded: nowhere near the runaway ceiling despite the hour-long deadline.
    assert handle.frame_calls < 500, (
        f"re-poll loop is not bounded by a poll cap: {handle.frame_calls} calls"
    )


def test_capture_body_frame_hermetic_independent_of_max_wait():
    """Hermetic: iteration count must not scale with the wall-clock deadline.

    A tiny and an enormous ``max_wait`` must both terminate at the same poll cap
    when frame() returns instantly — proving termination is input-driven, not
    time-driven.
    """
    from juggle_smoke import capture_body_frame

    short = _CountingBlankHandle(runaway=5000)
    long = _CountingBlankHandle(runaway=5000)
    capture_body_frame(short, rows=short.rows, max_wait=1.0)
    capture_body_frame(long, rows=long.rows, max_wait=3600.0)

    assert short.frame_calls == long.frame_calls, (
        "poll count depends on wall-clock deadline (non-hermetic): "
        f"{short.frame_calls} vs {long.frame_calls}"
    )


def test_capture_body_frame_still_captures_late_body_paint():
    """Regression: the poll cap must be generous enough that a body which paints
    on a later frame is still captured (the cap only guards runaway spins)."""
    from juggle_smoke import capture_body_frame, check_real_estate

    handle = _CountingBlankHandle(body_at_call=4)
    grid = capture_body_frame(handle, rows=handle.rows, max_wait=3600.0)

    assert check_real_estate(grid, handle.rows)["pass"], (
        "late-painting body was not captured before the poll cap stopped the loop"
    )
