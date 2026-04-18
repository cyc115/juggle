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
        """Split the first window to create a new pane. Returns pane_id like '%5'."""
        result = self._run_tmux(
            "split-window",
            "-t", self._first_window(),
            "-v", "-P", "-F", "#{pane_id}",
        )
        return result.stdout.strip()

    def start_claude_in_pane(self, pane_id: str, model: str | None = None) -> None:
        """Send the 'claude' command to a pane.

        Prefixes with env -u CLAUDE_PLUGIN_DATA to prevent DB fragmentation.
        """
        cmd = _get_settings()["agent"]["claude_launch_command"]
        if model:
            cmd += f" --model {model}"
        cmd = f"env -u CLAUDE_PLUGIN_DATA {cmd}"
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
        if os.environ.get("JUGGLE_TMUX_MOCK_SEND") == "1":
            return
        tmp = f"/tmp/juggle_task_{uuid.uuid4().hex[:8]}.txt"
        Path(tmp).write_text(prompt)

        if is_new:
            # Background subprocess: survives parent exit, handles delays
            script = (
                f"sleep 5; "
                f"tmux load-buffer -b juggle '{tmp}'; "
                f"tmux paste-buffer -b juggle -t '{pane_id}'; "
                f"tmux send-keys -t '{pane_id}' C-m; "
                f"sleep 10; "
                f"tmux send-keys -t '{pane_id}' C-m; "
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
                self._run_tmux("load-buffer", "-b", "juggle", tmp)
                self._run_tmux("paste-buffer", "-b", "juggle", "-t", pane_id)
                time.sleep(1)  # let Claude Code process pasted input
                self._run_tmux("send-keys", "-t", pane_id, "C-m")
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
