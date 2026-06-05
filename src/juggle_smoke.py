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

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path

import pyte
import yaml

_SCRIPT = Path(__file__).parent / "juggle_cockpit.py"

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
# PTY handle
# ---------------------------------------------------------------------------


class CockpitHandle:
    """PTY-backed interactive handle to a running cockpit process.

    Open via open_cockpit_pty(); use as a context manager for clean teardown.
    """

    def __init__(
        self,
        master_fd: int,
        proc: subprocess.Popen,
        screen: pyte.Screen,
        stream: pyte.Stream,
        cols: int,
        rows: int,
    ) -> None:
        self._master_fd = master_fd
        self._proc = proc
        self._screen = screen
        self._stream = stream
        self._cols = cols
        self._rows = rows

    # -- input ---------------------------------------------------------------

    def send(self, key: bytes | str) -> None:
        """Write key bytes to the pty master fd (real terminal input path)."""
        if isinstance(key, str):
            key = key.encode()
        os.write(self._master_fd, key)

    # -- output --------------------------------------------------------------

    def _read_available(self, settle: float) -> bytes:
        """Drain master fd for `settle` seconds, stopping early when quiet."""
        data = b""
        deadline = time.monotonic() + settle
        idle_deadline = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            remaining = deadline - now
            r, _, _ = select.select([self._master_fd], [], [], min(0.05, remaining))
            if r:
                try:
                    chunk = os.read(self._master_fd, 4096)
                    if chunk:
                        data += chunk
                        idle_deadline = time.monotonic() + 0.15
                    else:
                        break
                except OSError:
                    break
            else:
                # Nothing on fd; if we've been idle long enough after seeing
                # data, consider the frame stable
                if data and idle_deadline and time.monotonic() >= idle_deadline:
                    break
        return data

    def frame(self, settle: float = 1.0, timeout: float = 10.0) -> list[str]:
        """Capture a stable rendered frame as list[str] (one str per row).

        Polls until output stabilises (nothing new for 150ms) or timeout.
        Returns pyte.Screen.display — plain text, cols×rows, no ANSI codes.

        Stability requires footer chrome to be visible (keybinding bar rendered)
        so we don't lock onto a partial first paint where only header+border appear.
        """
        deadline = time.monotonic() + timeout
        prev_display: list[str] = []
        stable_count = 0

        while time.monotonic() < deadline:
            chunk = self._read_available(settle=min(settle, 0.3))
            if chunk:
                self._stream.feed(chunk.decode("utf-8", errors="replace"))
            current = list(self._screen.display)
            footer_ready = bool(current) and any(
                m in current[-1] for m in _FOOTER_MARKERS
            )
            if current == prev_display and any(ln.strip() for ln in current) and footer_ready:
                stable_count += 1
                if stable_count >= 2:
                    break
            else:
                stable_count = 0
            prev_display = current

        return list(self._screen.display)

    # -- resize --------------------------------------------------------------

    def resize(self, cols: int, rows: int) -> None:
        """Change the pty window size mid-session and send SIGWINCH."""
        self._cols = cols
        self._rows = rows
        self._screen.resize(rows, cols)
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        try:
            os.kill(self._proc.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    # -- lifecycle -----------------------------------------------------------

    def close(self, timeout: float = 4.0) -> None:
        """Send q, wait for clean exit, kill if timeout."""
        try:
            os.write(self._master_fd, b"q")
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        try:
            os.close(self._master_fd)
        except OSError:
            pass

    def __enter__(self) -> "CockpitHandle":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def open_cockpit_pty(
    profile: dict,
    db_path: str | None = None,
) -> CockpitHandle:
    """Spawn juggle_cockpit.py in a pty sized to (cols, rows) and return a handle.

    The slave fd is a real TTY so Textual renders in full TUI mode.
    TIOCSWINSZ is set before the child starts so it sees the correct size.
    """
    cols: int = profile["cols"]
    rows: int = profile["rows"]

    master_fd, slave_fd = pty.openpty()

    # Set terminal window size before spawning child
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass

    cmd = ["uv", "run", str(_SCRIPT)]
    if db_path:
        cmd += ["--db", db_path]

    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}

    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env=env,
    )
    os.close(slave_fd)  # parent keeps master_fd only

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)

    return CockpitHandle(master_fd, proc, screen, stream, cols, rows)


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
                grid = handle.frame(settle=2.0, timeout=12.0)

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
