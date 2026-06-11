"""juggle_smoke_pty — PTY-backed cockpit handle for the viewport smoke harness.

Owns: CockpitHandle (frame capture / input / resize over a real pty) and
open_cockpit_pty (factory that spawns juggle_cockpit.py sized via TIOCSWINSZ).
Must not own: smoke heuristics or the runner (juggle_smoke).
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

_SCRIPT = Path(__file__).parent / "juggle_cockpit.py"

# Footer chrome markers — frame() stability requires the keybinding bar
_FOOTER_MARKERS = ("Quit", "quit", "Help", "Switch", "Filter", "Ack", "Archive")


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
        """Send ctrl+c, wait for clean exit, kill whole process group on timeout.

        Idempotent: safe to call multiple times.
        """
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            os.write(self._master_fd, b"\x03")  # ctrl+c — real quit key
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass
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
    env: dict | None = None,
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

    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows), **(env or {})}

    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env=env,
        start_new_session=True,
    )
    os.close(slave_fd)  # parent keeps master_fd only

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)

    return CockpitHandle(master_fd, proc, screen, stream, cols, rows)
