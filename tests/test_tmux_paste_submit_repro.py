"""Repro + regression pin for the 2026-06-21 send-task paste-without-submit bug.

A tmux pane runs a tiny bracketed-paste-aware stub that logs raw input bytes.
We drive the SAME paste mechanic ``send_task`` uses
(``JuggleTmuxManager._paste_buffer`` + ``send-keys C-m``) across N>=20 iterations
of small / large / multiline prompts with timing jitter, then classify the
received byte stream:

  * a CR/LF received OUTSIDE a bracketed paste (ESC[200~ .. ESC[201~) == a submit
  * bytes inside the bracket == paste text (newlines must NOT submit)

Reliable submission == exactly ONE out-of-bracket submit per iteration, with the
full prompt received as paste text.

Pre-fix (``paste-buffer`` without ``-p``/``-r``) this FAILS: no bracket markers
are emitted and tmux translates LF->CR, so every embedded newline becomes a
premature submit and there is no clean paste/submit boundary (payloads empty).
"""
import shutil
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux required for paste/submit repro"
)

# Raw-mode stub: enable bracketed paste, log every received byte until Ctrl-D.
_STUB = r'''
import sys, os, tty, termios
log = open(sys.argv[1], "wb")
fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
sys.stdout.write("\x1b[?2004h"); sys.stdout.flush()
try:
    while True:
        b = os.read(fd, 4096)
        if not b:
            break
        log.write(b); log.flush()
        if b"\x04" in b:
            break
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    log.close()
'''


def _classify(raw: bytes):
    """Return (out_of_bracket_submits, [paste_payloads]) from a raw byte stream."""
    i = 0
    in_paste = False
    submits = 0
    payloads = []
    cur = b""
    while i < len(raw):
        if raw[i:i + 6] == b"\x1b[200~":
            in_paste = True
            cur = b""
            i += 6
            continue
        if raw[i:i + 6] == b"\x1b[201~":
            in_paste = False
            payloads.append(cur)
            i += 6
            continue
        ch = raw[i:i + 1]
        if in_paste:
            cur += ch
        elif ch in (b"\r", b"\n"):
            submits += 1
        i += 1
    return submits, payloads


PROMPTS = [
    "go",                                       # tiny single-line
    "implement the feature now please",         # longer single-line
    "line one\nline two\nline three",           # multiline
    "first\n\nsecond\n\n\nthird",               # blank lines between
    "x" * 5000,                                 # large single-line
    "paragraph\n" * 200,                        # large multiline
    "weird $chars `~!@#%^&*() and \"quotes\" and 'apos'",
]


def test_paste_submit_100pct(tmp_path):
    """2026-06-21 send-task paste-without-submit: N>=20 iters submit 100% (0 stuck)."""
    from juggle_tmux import JuggleTmuxManager

    log = tmp_path / "rx.bin"
    stub = tmp_path / "stub.py"
    stub.write_text(_STUB)
    session = f"jpsr_{uuid.uuid4().hex[:8]}"
    mgr = JuggleTmuxManager(session_name=session)

    jitter = [0.0, 0.05, 0.1, 0.2]
    plan = [(PROMPTS[k % len(PROMPTS)], jitter[k % len(jitter)]) for k in range(21)]

    mgr._run_tmux("new-session", "-s", session, "-d", "-x", "200", "-y", "50")
    try:
        panes = mgr._run_tmux(
            "list-panes", "-t", session, "-F", "#{pane_id}"
        ).stdout.strip().splitlines()
        pane = panes[0]
        mgr._run_tmux("send-keys", "-t", pane, f"{sys.executable} {stub} {log}", "Enter")
        time.sleep(1.0)  # stub up + bracketed-paste mode enabled

        src = tmp_path / "p.txt"
        for prompt, jit in plan:
            src.write_text(prompt)
            mgr._paste_buffer(pane, str(src))
            if jit:
                time.sleep(jit)
            mgr._run_tmux("send-keys", "-t", pane, "C-m")
            time.sleep(0.15)
        time.sleep(0.3)
        mgr._run_tmux("send-keys", "-t", pane, "C-d")
        time.sleep(0.3)
    finally:
        mgr._run_tmux("kill-session", "-t", session)

    raw = log.read_bytes()
    submits, payloads = _classify(raw)
    n = len(plan)

    assert submits == n, (
        f"expected {n} clean out-of-bracket submits, got {submits} "
        "(embedded newlines submitting / C-m absorbed by paste)"
    )
    assert len(payloads) == n, (
        f"expected {n} bracketed pastes, got {len(payloads)} "
        "(paste-buffer not emitting bracketed-paste markers)"
    )
    for (prompt, _jit), payload in zip(plan, payloads):
        assert payload.decode() == prompt, "paste payload mangled (LF->CR or split)"
