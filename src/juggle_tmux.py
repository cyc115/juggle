#!/usr/bin/env python3
"""Juggle Tmux Manager — persistent agent pool via tmux panes."""

import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

from juggle_settings import get_settings as _get_settings

# Built-in Claude Code markers — used as the fallback when the configured
# harness adapter can't be resolved (see _harness_markers).
_READY_MARKERS = ("bypass permissions on", "/effort")
_SUBMISSION_MARKERS = ("esc to interrupt", "✻", "✶")
_DETECT_TAIL_LINES = 10  # lines of scrollback tail used for submission/stuck detection
_PROMPT_HEAD_CHARS = 40


def _harness_markers():
    """Return ``(readiness, submission)`` marker tuples for the default harness.

    Resolved from the GLOBAL default harness (``agent.harness``) via
    ``juggle_harness.get_adapter`` — panes don't carry their harness id, so a
    mixed per-role harness setup shares these markers. Falls back to the
    built-in Claude markers if the adapter can't be resolved.
    """
    try:
        from juggle_harness import get_adapter

        adapter = get_adapter()
        return (
            adapter.readiness_markers() or _READY_MARKERS,
            adapter.submission_markers() or _SUBMISSION_MARKERS,
        )
    except Exception:
        return _READY_MARKERS, _SUBMISSION_MARKERS


class JuggleTmuxManager:
    def __init__(self, session_name: str | None = None):
        self.session_name = session_name or _get_settings()["tmux"]["session_name"]

    def _run_tmux(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux"] + list(args),
            capture_output=True,
            text=True,
        )

    def ensure_session(self) -> None:
        """Create the juggle tmux session + window 0 if not already running."""
        try:
            result = self._run_tmux("has-session", "-t", self.session_name)
        except FileNotFoundError:
            raise RuntimeError("tmux not found. Install tmux to use persistent agents.")
        if result.returncode != 0:
            _s = _get_settings()["tmux"]
            self._run_tmux(
                "new-session",
                "-s",
                self.session_name,
                "-d",
                "-x",
                str(_s["session_width"]),
                "-y",
                str(_s["session_height"]),
            )

    def _first_window(self) -> str:
        """Return the target string for the first window (respects base-index)."""
        result = self._run_tmux(
            "list-windows", "-t", self.session_name, "-F", "#{window_index}"
        )
        first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "0"
        return f"{self.session_name}:{first}"

    def spawn_pane(self) -> str:
        """Split the first window to create a new pane. Returns pane_id like '%5'.

        Falls back to new-window when the window is too small to split.
        """
        result = self._run_tmux(
            "split-window",
            "-t",
            self._first_window(),
            "-v",
            "-P",
            "-F",
            "#{pane_id}",
        )
        pane_id = result.stdout.strip()
        if not pane_id:
            if "no space" in result.stderr:
                # Window too small to split — create a new window instead.
                result = self._run_tmux(
                    "new-window",
                    "-t",
                    self.session_name,
                    "-P",
                    "-F",
                    "#{pane_id}",
                )
                pane_id = result.stdout.strip()
            if not pane_id:
                raise RuntimeError(
                    f"spawn_pane failed: could not create pane via split-window or new-window. "
                    f"stderr={result.stderr!r}"
                )
        return pane_id

    def start_agent_in_pane(
        self, pane_id: str, model: str | None = None, role: str | None = None
    ) -> None:
        """Launch the configured agent harness in a pane.

        Command construction is delegated to the role's ``HarnessAdapter``
        (``juggle_harness``), so the binary, flags, per-role tool restrictions
        and env scrubbing are all harness-specific and config-driven. The
        default harness is Claude Code: ``env -u CLAUDE_PLUGIN_DATA`` to prevent
        DB fragmentation, and per-role denied tools written to a settings
        overlay file passed via ``--settings <path>`` (a short, fixed token that
        pastes reliably; ``--settings`` layers additively over the host
        hierarchy so it never replaces the host's settings).

        The command is written to a temp file and pasted via tmux
        load-buffer/paste-buffer rather than typed — tmux collapses big pastes,
        so a fixed-length buffer token is reliable where a long command line is
        not.
        """
        from juggle_harness import get_adapter

        agent_cfg = _get_settings().get("agent", {})
        adapter = get_adapter(role, agent_cfg=agent_cfg)
        cmd = adapter.build_launch_command(
            role=role, model=model, audit=bool(agent_cfg.get("audit_mode"))
        )

        tmp = f"/tmp/juggle_launch_{uuid.uuid4().hex[:8]}.txt"
        buf_name = f"juggle_{uuid.uuid4().hex[:8]}"
        try:
            Path(tmp).write_text(cmd)
            self._run_tmux("load-buffer", "-b", buf_name, tmp)
            self._run_tmux("paste-buffer", "-b", buf_name, "-t", pane_id)
            self._run_tmux("delete-buffer", "-b", buf_name)
            self._run_tmux("send-keys", "-t", pane_id, "Enter")
        finally:
            if Path(tmp).exists():
                os.unlink(tmp)

    # Back-compat alias: the historical name is still used by callers and tests.
    # Harness-neutral name is start_agent_in_pane; both refer to the same method.
    start_claude_in_pane = start_agent_in_pane

    def verify_pane(self, pane_id: str) -> bool:
        """Return True if pane_id exists in the juggle session."""
        if os.environ.get("JUGGLE_TMUX_MOCK_PANE") or os.environ.get(
            "JUGGLE_TMUX_MOCK_SEND"
        ):
            return True
        result = self._run_tmux(
            "list-panes", "-t", self._first_window(), "-F", "#{pane_id}"
        )
        return pane_id in result.stdout.splitlines()

    def kill_pane(self, pane_id: str) -> None:
        """Kill a tmux pane. No-op if JUGGLE_TMUX_MOCK_KILL=1."""
        if os.environ.get("JUGGLE_TMUX_MOCK_KILL") == "1":
            return
        self._run_tmux("kill-pane", "-t", pane_id)

    def wait_for_ready_to_paste(
        self,
        pane_id: str,
        attempts: int | None = None,
        interval: float | None = None,
    ) -> bool:
        """Poll capture-pane until a Claude UI readiness marker appears.

        Backoff: check the pane up to `attempts` times, sleeping `interval`
        seconds between checks (total wait ≈ attempts × interval). Returns True
        once any marker in _READY_MARKERS appears; False once all attempts are
        exhausted. Both knobs default from settings (`tmux.ready_poll_attempts`
        / `tmux.ready_poll_interval_secs`) and are overridable via the
        JUGGLE_READY_POLL_ATTEMPTS / JUGGLE_READY_POLL_INTERVAL_SECS env vars.
        """
        tmux_cfg = _get_settings()["tmux"]
        if attempts is None:
            attempts = tmux_cfg["ready_poll_attempts"]
        if interval is None:
            interval = tmux_cfg["ready_poll_interval_secs"]
        attempts = max(1, int(attempts))
        ready_markers, _ = _harness_markers()
        for i in range(attempts):
            result = self._run_tmux("capture-pane", "-pt", pane_id)
            out = getattr(result, "stdout", "") or ""
            if any(m in out for m in ready_markers):
                return True
            if i < attempts - 1:  # don't sleep after the final attempt
                time.sleep(interval)
        return False

    def wait_for_submission(
        self,
        pane_id: str,
        pasted_prompt: str,
        timeout: int = 15,
        max_enter_retries: int = 5,
    ) -> bool:
        """Verify a pasted prompt was submitted; retry Enter if stuck.

        Success: a _SUBMISSION_MARKERS token ("esc to interrupt" / "✻" / "✶")
        appears in the pane output.

        Stuck: the bottom region still contains input — either a
        "[Pasted text" collapsed-paste placeholder, the first 40 chars of
        the prompt (short prompts), or a non-empty ❯/> prompt line. Sends
        C-m on each stuck poll up to max_enter_retries.

        NOTE: the old "head not in bottom → True" branch has been removed.
        Claude Code collapses large pastes into "[Pasted text #N +M lines]"
        so the head is never present in the bottom, causing an immediate
        false-positive that left tasks unsubmitted at the prompt.

        Returns True on success, False on timeout.
        """
        first_line = (
            pasted_prompt.strip().split("\n", 1)[0] if pasted_prompt.strip() else ""
        )
        head = first_line[:_PROMPT_HEAD_CHARS]
        _, submission_markers = _harness_markers()

        retries = 0
        for _ in range(max(1, timeout)):
            result = self._run_tmux(
                "capture-pane", "-p", "-t", pane_id, "-S", f"-{_DETECT_TAIL_LINES}"
            )
            out = getattr(result, "stdout", "") or ""
            tail = out.splitlines()[-_DETECT_TAIL_LINES:]
            if any(m in line for m in submission_markers for line in tail):
                return True
            bottom = "\n".join(tail)
            stuck = (
                "[Pasted text" in bottom
                or "-- INSERT --" in bottom
                or (head and head in bottom)
                or any(
                    line.strip().startswith(("❯ ", "> ")) and len(line.strip()) > 2
                    for line in bottom.splitlines()
                )
            )
            if stuck and retries < max_enter_retries:
                if "-- INSERT --" in bottom:
                    self._run_tmux("send-keys", "-t", pane_id, "Escape")
                    time.sleep(0.1)
                self._run_tmux("send-keys", "-t", pane_id, "C-m")
                retries += 1
            time.sleep(1)
        return False

    def send_task(self, pane_id: str, prompt: str, is_new: bool = False) -> str:
        """Send a task prompt to an agent pane via tmux load-buffer + paste-buffer.

        Uses a temp file to avoid shell-escaping issues with multi-line prompts.
        Unified flow for both new and reused agents:
          1. wait_for_ready_to_paste — block until the Claude UI is up.
          2. load-buffer + paste-buffer (captures pane hash) + send-keys C-m.
          3. wait_for_submission — verify the prompt left the input box; retry Enter if not.

        Returns a 16-hex-char SHA-256 of the post-paste-pre-Enter pane tail.
        `is_new` is accepted for caller backward compatibility but is no longer
        consulted — the wait helpers handle both cold-start and mid-render cases.
        Returns a deterministic mock hash if JUGGLE_TMUX_MOCK_SEND=1.
        """
        import hashlib as _hashlib
        import time as _time

        del is_new
        if not pane_id or not pane_id.strip():
            raise ValueError(
                "send_task called with empty pane_id — aborting to avoid pasting to wrong tmux session"
            )
        if os.environ.get("JUGGLE_TMUX_MOCK_SEND") == "1":
            return _hashlib.sha256(prompt.encode()).hexdigest()[:16]

        _tmux_cfg = _get_settings()["tmux"]
        _attempts = _tmux_cfg["ready_poll_attempts"]
        _interval = _tmux_cfg["ready_poll_interval_secs"]
        if not self.wait_for_ready_to_paste(pane_id):
            raise RuntimeError(
                f"Claude UI not ready in pane {pane_id} after "
                f"{_attempts}×{_interval:g}s (~{_attempts * _interval:g}s) — aborting send_task"
            )

        tmp = f"/tmp/juggle_task_{uuid.uuid4().hex[:8]}.txt"
        Path(tmp).write_text(prompt)
        buf_name = f"juggle_{uuid.uuid4().hex[:8]}"
        pane_hash = "0000000000000000"
        try:
            self._run_tmux("load-buffer", "-b", buf_name, tmp)
            self._run_tmux("paste-buffer", "-b", buf_name, "-t", pane_id)
            self._run_tmux("delete-buffer", "-b", buf_name)
            # Capture pane tail BEFORE sending Enter for stuck-at-prompt detection
            # 0.4s gives the TUI time to render the collapsed-paste placeholder
            # before we take the snapshot and send the first C-m.
            _time.sleep(0.4)
            cap = self._run_tmux("capture-pane", "-pt", pane_id, "-S", "-10")
            tail = (cap.stdout or "") if cap else ""
            pane_hash = _hashlib.sha256(tail.encode()).hexdigest()[:16]
            self._run_tmux("send-keys", "-t", pane_id, "C-m")
            if not self.wait_for_submission(pane_id, prompt, timeout=15):
                logging.warning(
                    "send_task: submission not verified for pane %s — may need manual retry",
                    pane_id,
                )
        finally:
            if Path(tmp).exists():
                os.unlink(tmp)
        return pane_hash

    def spawn_agent(self, db, role: str, model: str | None = None) -> dict:
        """Spawn a new claude pane, register in DB, return agent dict.

        db must be a JuggleDB instance with init_db() already called.
        Raises ValueError if pool is at MAX_BACKGROUND_AGENTS.
        Mock mode: if JUGGLE_TMUX_MOCK_PANE set, skip tmux and use that pane_id directly.
        """
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path(__file__).parent))
        from juggle_db import MAX_BACKGROUND_AGENTS

        agents = db.get_all_agents()
        if len(agents) >= MAX_BACKGROUND_AGENTS:
            raise ValueError(
                f"Agent pool full ({MAX_BACKGROUND_AGENTS} max). "
                "Wait for one to finish before spawning more."
            )

        mock_pane = os.environ.get("JUGGLE_TMUX_MOCK_PANE")
        if mock_pane:
            agent_id = db.create_agent(role=role, pane_id=mock_pane)
            return db.get_agent(agent_id)

        self.ensure_session()
        pane_id = self.spawn_pane()
        self.start_claude_in_pane(pane_id, model=model, role=role)

        agent_id = db.create_agent(role=role, pane_id=pane_id)
        return db.get_agent(agent_id)

    def get_pane_last_used(self, pane_id: str) -> int:
        """Return Unix timestamp of pane's last activity, or 0 on failure."""
        result = self._run_tmux("display", "-pt", pane_id, "#{pane_last_used}")
        raw = result.stdout.strip()
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 0

    def decommission_agent(self, db, agent_id: str) -> None:
        """Kill the agent's pane and remove it from the DB."""
        agent = db.get_agent(agent_id)
        if agent:
            self.kill_pane(agent["pane_id"])
            db.delete_agent(agent_id)


def _pane_has_juggle_agent_env(pane_id: str) -> bool:
    """Return True if any child process of the pane has JUGGLE_IS_AGENT=1."""
    import subprocess as _sp

    try:
        pane_pid = _sp.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if not pane_pid:
            return False
        children = (
            _sp.run(
                ["pgrep", "-P", pane_pid],
                capture_output=True,
                text=True,
                timeout=3,
            )
            .stdout.strip()
            .splitlines()
        )
        for child in children:
            env_out = _sp.run(
                ["ps", "eww", "-p", child],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
            if "JUGGLE_IS_AGENT=1" in env_out:
                return True
    except Exception:
        pass
    return False


def reap_stale_agents(db, mgr):
    """Reap agents idle longer than agent_idle_ttl_secs.

    Always reaps agents whose tmux pane no longer exists, regardless of status.
    Also kills unowned panes (JUGGLE_IS_AGENT=1 but no DB record) to handle
    DB-reset/migration scenarios where pane state outlives DB state.
    Skips busy (live-pane) agents and agents assigned to the current thread.
    Returns count of agents reaped.
    """
    from datetime import datetime, timezone
    from juggle_settings import get_settings

    settings = get_settings()
    ttl_secs = settings["agent_idle_ttl_secs"]
    current_thread = db.get_current_thread()

    now_ts = datetime.now(timezone.utc)
    reaped = 0

    # DB→tmux: reap DB entries whose panes are gone or past TTL.
    for a in db.get_all_agents():
        # Always reap agents whose pane no longer exists, regardless of status
        if not mgr.verify_pane(a["pane_id"]):
            db.delete_agent(a["id"])
            reaped += 1
            continue

        if a["status"] != "idle" or a["assigned_thread"] == current_thread:
            continue

        last_active = a.get("last_active") or ""
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now_ts - dt).total_seconds() > ttl_secs:
                    mgr.decommission_agent(db, a["id"])
                    reaped += 1
            except (ValueError, TypeError):
                pass

    # tmux→DB: kill panes tagged JUGGLE_IS_AGENT=1 with no DB record.
    # Handles DB-reset/migration scenarios where panes outlive their DB entries.
    known_pane_ids = {a["pane_id"] for a in db.get_all_agents()}
    try:
        import subprocess as _sp

        result = _sp.run(
            ["tmux", "list-panes", "-t", mgr.session_name, "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for pane_id in result.stdout.strip().splitlines():
            if pane_id in known_pane_ids:
                continue
            if _pane_has_juggle_agent_env(pane_id):
                mgr.kill_pane(pane_id)
                reaped += 1
    except Exception:
        pass

    return reaped
