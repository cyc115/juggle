#!/usr/bin/env python3
"""Juggle Tmux Manager — persistent agent pool via tmux panes."""

import os
import subprocess
import time
import uuid
from pathlib import Path

from juggle_settings import get_settings as _get_settings


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
                "new-session", "-s", self.session_name,
                "-d", "-x", str(_s["session_width"]), "-y", str(_s["session_height"]),
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
            "-t", self._first_window(),
            "-v", "-P", "-F", "#{pane_id}",
        )
        pane_id = result.stdout.strip()
        if not pane_id:
            if "no space" in result.stderr:
                # Window too small to split — create a new window instead.
                result = self._run_tmux(
                    "new-window",
                    "-t", self.session_name,
                    "-P", "-F", "#{pane_id}",
                )
                pane_id = result.stdout.strip()
            if not pane_id:
                raise RuntimeError(
                    f"spawn_pane failed: could not create pane via split-window or new-window. "
                    f"stderr={result.stderr!r}"
                )
        return pane_id

    def start_claude_in_pane(self, pane_id: str, model: str | None = None) -> None:
        """Send the 'claude' command to a pane.

        Prefixes with env -u CLAUDE_PLUGIN_DATA to prevent DB fragmentation.
        """
        cmd = _get_settings()["agent"]["claude_launch_command"]
        if model:
            cmd += f" --model {model}"
        cmd = f"env -u CLAUDE_PLUGIN_DATA JUGGLE_IS_AGENT=1 {cmd}"
        self._run_tmux("send-keys", "-t", pane_id, cmd, "Enter")

    def verify_pane(self, pane_id: str) -> bool:
        """Return True if pane_id exists in the juggle session."""
        if os.environ.get("JUGGLE_TMUX_MOCK_PANE") or os.environ.get("JUGGLE_TMUX_MOCK_SEND"):
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

    def send_task(self, pane_id: str, prompt: str, is_new: bool = False) -> None:
        """Send a task prompt to an agent pane via tmux load-buffer + paste-buffer.

        Uses a temp file to avoid shell-escaping issues with multi-line prompts.
        For new agents (is_new=True): spawns a background subprocess that waits
        for Claude Code to start, pastes, and retries Enter after 10s.
        For existing agents: sends synchronously.
        No-op if JUGGLE_TMUX_MOCK_SEND=1.
        """
        if not pane_id or not pane_id.strip():
            raise ValueError(f"send_task called with empty pane_id — aborting to avoid pasting to wrong tmux session")
        if os.environ.get("JUGGLE_TMUX_MOCK_SEND") == "1":
            return
        tmp = f"/tmp/juggle_task_{uuid.uuid4().hex[:8]}.txt"
        Path(tmp).write_text(prompt)
        buf_name = f"juggle_{uuid.uuid4().hex[:8]}"

        if is_new:
            # Background subprocess: survives parent exit, handles delays
            script = (
                f"sleep 5; "
                f"tmux load-buffer -b {buf_name} '{tmp}'; "
                f"tmux paste-buffer -b {buf_name} -t '{pane_id}'; "
                f"tmux send-keys -t '{pane_id}' C-m; "
                f"sleep 10; "
                f"tmux send-keys -t '{pane_id}' C-m; "
                f"tmux delete-buffer -b {buf_name}; "
                f"rm -f '{tmp}'"
            )
            subprocess.Popen(
                ["bash", "-c", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            try:
                self._run_tmux("load-buffer", "-b", buf_name, tmp)
                self._run_tmux("paste-buffer", "-b", buf_name, "-t", pane_id)
                time.sleep(1)  # let Claude Code process pasted input
                self._run_tmux("send-keys", "-t", pane_id, "C-m")
                self._run_tmux("delete-buffer", "-b", buf_name)
                # Retry Enter after 5s in case first was swallowed
                subprocess.Popen(
                    ["bash", "-c", f"sleep 5; tmux send-keys -t '{pane_id}' C-m"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            finally:
                if Path(tmp).exists():
                    os.unlink(tmp)

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
        self.start_claude_in_pane(pane_id, model=model)

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
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if not pane_pid:
            return False
        children = _sp.run(
            ["pgrep", "-P", pane_pid],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip().splitlines()
        for child in children:
            env_out = _sp.run(
                ["ps", "eww", "-p", child],
                capture_output=True, text=True, timeout=3,
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
            capture_output=True, text=True, timeout=5,
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
