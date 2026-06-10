"""Juggle cockpit viewport smoke harness.

Mechanism: pty+pyte (primary). Spawns `juggle_cockpit.py` in a real
pseudo-terminal sized to the requested viewport via TIOCSWINSZ, drives
it with raw key bytes written to the pty master fd, and processes ANSI
output through a pyte Screen for a deterministic cols×rows text grid.

Fallback: if the pty path is non-deterministic in a given env (e.g. a
strict CI headless environment), set SMOKE_SKIP=1 to skip pty-based
tests; the pure-function heuristic tests and viewport loader tests
always run regardless.

API:
    handle = open_cockpit_pty(profile, db_path=...)
    with handle:
        grid = handle.frame(settle=2.0, timeout=10.0)  # list[str]
        handle.send(b"j")                               # key input
        handle.resize(80, 67)                           # mid-session resize
"""
from __future__ import annotations

import time
from pathlib import Path

import yaml

from juggle_smoke_pty import (  # noqa: F401 — re-exported public API
    CockpitHandle,
    open_cockpit_pty,
)

# Chrome detection markers
_HEADER_MARKERS = ("Juggle", "Cockpit", "juggle")
_FOOTER_MARKERS = ("Quit", "quit", "Help", "Switch", "Filter", "Ack", "Archive")


# ---------------------------------------------------------------------------
# Viewport loader
# ---------------------------------------------------------------------------


def load_viewports(path: str | Path) -> dict:
    """Load viewport profiles from a YAML file.

    Returns dict[name -> {"cols": int, "rows": int, "desc": str}].
    Raises FileNotFoundError if path does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Viewports config not found: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("profiles", {})


# ---------------------------------------------------------------------------
# Pure heuristics
# ---------------------------------------------------------------------------


def check_overflow(grid: list[str], cols: int) -> dict:
    """No rendered line's visible width may exceed `cols`.

    Returns {"pass": bool, "violations": list[str]}.
    """
    violations: list[str] = []
    for i, line in enumerate(grid):
        if len(line) > cols:
            violations.append(f"row {i}: len={len(line)} > cols={cols}: {line[:40]!r}…")
    return {"pass": len(violations) == 0, "violations": violations}


def check_real_estate(grid: list[str], rows: int) -> dict:
    """Flag if >40% of rows are entirely blank (wasted space).

    Returns {"pass": bool, "blank_pct": float, "content_pct": float, "reason": str}.
    """
    total = len(grid)
    if total == 0:
        return {"pass": False, "blank_pct": 1.0, "content_pct": 0.0, "reason": "empty grid"}
    blank = sum(1 for ln in grid if not ln.strip())
    blank_pct = blank / total
    content_pct = 1.0 - blank_pct
    ok = blank_pct <= 0.40
    reason = "" if ok else f"blank_pct={blank_pct:.0%} > 40% threshold"
    return {"pass": ok, "blank_pct": blank_pct, "content_pct": content_pct, "reason": reason}


def check_chrome_present(grid: list[str]) -> dict:
    """Header (top 3 rows) and footer (bottom 3 rows) must render.

    Header marker: app title "Juggle" / "Cockpit".
    Footer marker: any visible keybinding label.

    Returns {"pass": bool, "reason": str}.
    """
    if not grid:
        return {"pass": False, "reason": "empty grid"}
    top = grid[:3]
    bottom = grid[-3:]
    has_header = any(m in row for row in top for m in _HEADER_MARKERS)
    has_footer = any(m in row for row in bottom for m in _FOOTER_MARKERS)
    ok = has_header and has_footer
    parts = []
    if not has_header:
        parts.append("header MISSING")
    if not has_footer:
        parts.append("footer MISSING")
    return {"pass": ok, "reason": ", ".join(parts) if parts else ""}


def check_truncation(grid: list[str]) -> dict:
    """Count ellipsis (…) truncation markers across all rows.

    Returns {"warn": bool, "count": int}. Not a hard fail — informational.
    """
    count = sum(line.count("…") for line in grid)
    return {"warn": count > 0, "count": count}


# ---------------------------------------------------------------------------
# Smoke runner
# ---------------------------------------------------------------------------

# The cockpit's chrome (header/footer) paints within ~1s of launch, but the
# body panes can take several more seconds (DB load + Textual layout). A
# single settle-based capture locks onto the blank first paint and falsely
# fails real-estate (incident 2026-06-10: all 7 profiles blank_pct=94%).
_BODY_PAINT_DEADLINE = 25.0  # max seconds to wait for body content
_BODY_BLANK_THRESHOLD = 0.40  # matches check_real_estate pass criterion


def capture_body_frame(
    handle: CockpitHandle,
    rows: int,
    max_wait: float | None = None,
    blank_threshold: float = _BODY_BLANK_THRESHOLD,
) -> list[str]:
    """Capture frames until the body has painted or a bounded deadline passes.

    Re-polls handle.frame() while the grid is mostly blank (blank_pct above
    `blank_threshold`). Returns the last captured grid either way, so a
    genuinely blank layout still fails check_real_estate downstream.
    """
    if max_wait is None:
        max_wait = _BODY_PAINT_DEADLINE
    deadline = time.monotonic() + max_wait
    grid = handle.frame(settle=2.0, timeout=12.0)
    while (
        check_real_estate(grid, rows)["blank_pct"] > blank_threshold
        and time.monotonic() < deadline
    ):
        grid = handle.frame(settle=1.0, timeout=4.0)
    return grid


def run_smoke(
    profiles: dict,
    db_path: str | None = None,
    output_dir: Path | None = None,
    interactive: bool = False,
) -> list[dict]:
    """Render each viewport profile, run heuristics, dump frames.

    Returns list of per-profile result dicts with keys:
      profile, cols, rows, pass, overflow, real_estate, chrome, truncation,
      frame_file, error (if any).
    """
    if output_dir is None:
        output_dir = Path("data/cockpit-viewport-review")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for name, profile in profiles.items():
        cols, rows = profile["cols"], profile["rows"]
        rec: dict = {"profile": name, "cols": cols, "rows": rows}
        try:
            with open_cockpit_pty(profile, db_path=db_path) as handle:
                grid = capture_body_frame(handle, rows)

                if interactive:
                    # Nav: scroll down
                    for _ in range(5):
                        handle.send(b"j")
                    handle.frame(settle=0.5, timeout=3.0)
                    # Resize transition: shrink to 2k_third dims
                    handle.resize(80, 67)
                    grid_small = handle.frame(settle=1.5, timeout=8.0)
                    rec["resize_overflow"] = check_overflow(grid_small, 80)
                    # Restore original size
                    handle.resize(cols, rows)
                    handle.frame(settle=0.5, timeout=3.0)
                    # Flow: Tab cycle pane
                    handle.send(b"\t")
                    handle.frame(settle=0.5, timeout=3.0)

            rec["overflow"] = check_overflow(grid, cols)
            rec["real_estate"] = check_real_estate(grid, rows)
            rec["chrome"] = check_chrome_present(grid)
            rec["truncation"] = check_truncation(grid)
            rec["pass"] = (
                rec["overflow"]["pass"]
                and rec["real_estate"]["pass"]
                and rec["chrome"]["pass"]
            )

            frame_path = output_dir / f"{name}.txt"
            frame_path.write_text("\n".join(grid) + "\n", encoding="utf-8")
            rec["frame_file"] = str(frame_path)

        except Exception as exc:
            rec["pass"] = False
            rec["error"] = str(exc)

        results.append(rec)

    return results
