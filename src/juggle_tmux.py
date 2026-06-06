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
        """Create a new window for the agent. Returns pane_id like '%5'.

        Uses new-window so each agent gets a full-size terminal independent of
        any existing panes. Splitting the first window produces tiny unusable
        panes when multiple agents are running concurrently.
        """
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
                f"spawn_pane failed: new-window returned no pane_id. "
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
        if not adapter.is_interactive:
            # One-shot harness (e.g. codex exec): nothing to launch up front — the
            # per-task process is spawned at send time (run_task_oneshot). The
            # pane stays a ready shell.
            return
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
            "list-panes", "-s", "-t", self.session_name, "-F", "#{pane_id}"
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

        Mock mode: when JUGGLE_TMUX_MOCK_NOT_READY_PANES is set (even to empty
        string), panes listed in the comma-separated value are considered NOT
        ready (return False); all other panes are ready (return True). No real
        tmux call is made.
        """
        _not_ready = os.environ.get("JUGGLE_TMUX_MOCK_NOT_READY_PANES", None)
        if _not_ready is not None:
            not_ready_set = set(p.strip() for p in _not_ready.split(",") if p.strip())
            return pane_id not in not_ready_set

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

    def send_message(self, pane_id: str, text: str) -> bool:
        """Send a steering message to an already-running agent pane.

        Unlike send_task, skips wait_for_ready_to_paste — the agent is expected
        to already be processing a task. Requires the pane to exist and have a
        live JUGGLE_IS_AGENT process.

        Raises RuntimeError if pane missing, process dead, or submission fails.
        """
        import time as _time

        if not pane_id or not pane_id.strip():
            raise ValueError("send_message called with empty pane_id")
        if os.environ.get("JUGGLE_TMUX_MOCK_SEND") == "1":
            return True

        if not self.verify_pane(pane_id):
            raise RuntimeError(
                f"Pane {pane_id} not found in session {self.session_name}"
            )
        if not _pane_has_juggle_agent_env(pane_id):
            raise RuntimeError(
                f"No live agent process in pane {pane_id} — send_message requires a running agent"
            )

        tmp = f"/tmp/juggle_msg_{uuid.uuid4().hex[:8]}.txt"
        buf_name = f"juggle_{uuid.uuid4().hex[:8]}"
        Path(tmp).write_text(text)
        try:
            self._run_tmux("load-buffer", "-b", buf_name, tmp)
            self._run_tmux("paste-buffer", "-b", buf_name, "-t", pane_id)
            self._run_tmux("delete-buffer", "-b", buf_name)
            _time.sleep(0.4)
            self._run_tmux("send-keys", "-t", pane_id, "C-m")
            if not self.wait_for_submission(pane_id, text, timeout=15):
                raise RuntimeError(
                    f"Message submission not verified for pane {pane_id} — Enter may not have landed"
                )
        finally:
            if Path(tmp).exists():
                os.unlink(tmp)
        return True

    def run_task_oneshot(
        self,
        pane_id: str,
        prompt: str,
        role: str | None = None,
        model: str | None = None,
        audit: bool = False,
    ):
        """Dispatch a task to a NON-interactive harness as a one-shot process.

        Simpler than ``send_task``: there is no warm REPL to wait on and no
        submission to verify. The prompt is written to a temp file, the harness'
        one-shot command (``adapter.build_task_command``) is pasted into the
        pane, and the process runs to completion and exits. We still paste via
        load-buffer/paste-buffer so the (short, fixed) command line is reliable,
        and return a (pane_hash, child_pid) tuple. child_pid is None if the PID
        could not be determined.

        Honours JUGGLE_TMUX_MOCK_SEND like ``send_task`` for tests.
        """
        import hashlib as _hashlib
        import subprocess as _sp
        import time as _time

        from juggle_harness import get_adapter

        if not pane_id or not pane_id.strip():
            raise ValueError("run_task_oneshot called with empty pane_id")
        if os.environ.get("JUGGLE_TMUX_MOCK_SEND") == "1":
            return _hashlib.sha256(prompt.encode()).hexdigest()[:16], None

        agent_cfg = _get_settings().get("agent", {})
        adapter = get_adapter(role, agent_cfg=agent_cfg)

        # The prompt is written to /tmp and read by the one-shot process via the
        # adapter's prompt_arg (stdin redirect). We deliberately leave the file in
        # place — the OS tmp reaper handles it, and keeping it makes the run
        # auditable (you can re-read exactly what the agent was given).
        prompt_tmp = f"/tmp/juggle_oneshot_{uuid.uuid4().hex[:8]}.txt"
        Path(prompt_tmp).write_text(prompt)
        cmd = adapter.build_task_command(
            prompt_tmp, role=role, model=model, audit=audit
        )

        cmd_tmp = f"/tmp/juggle_oneshotcmd_{uuid.uuid4().hex[:8]}.txt"
        buf_name = f"juggle_{uuid.uuid4().hex[:8]}"
        try:
            Path(cmd_tmp).write_text(cmd)
            self._run_tmux("load-buffer", "-b", buf_name, cmd_tmp)
            self._run_tmux("paste-buffer", "-b", buf_name, "-t", pane_id)
            self._run_tmux("delete-buffer", "-b", buf_name)
            self._run_tmux("send-keys", "-t", pane_id, "Enter")
            cap = self._run_tmux("capture-pane", "-pt", pane_id, "-S", "-10")
            tail = (getattr(cap, "stdout", "") or "")
            pane_hash = _hashlib.sha256(tail.encode()).hexdigest()[:16]

            # Resolve the child PID of the one-shot process. Poll a few short
            # attempts because there is a race between send-keys and process spawn.
            child_pid = None
            for _ in range(6):
                _time.sleep(0.15)
                pid = _get_oneshot_child_pid(pane_id)
                if pid is not None:
                    child_pid = pid
                    break

            return pane_hash, child_pid
        finally:
            if Path(cmd_tmp).exists():
                os.unlink(cmd_tmp)

    def spawn_agent(self, db, role: str, model: str | None = None) -> dict:
        """Spawn a new claude pane, register in DB, return agent dict.

        db must be a JuggleDB instance with init_db() already called.
        Raises ValueError if pool is at MAX_BACKGROUND_AGENTS.
        Mock mode: if JUGGLE_TMUX_MOCK_PANE set, skip tmux and use that pane_id directly.

        Tags the agent with the **launch-time** harness id so recycled panes
        (started under one harness, still running that REPL) display correctly
        even after a config switch.
        """
        import sys
        from pathlib import Path as _Path

        sys.path.insert(0, str(_Path(__file__).parent))
        from juggle_db import MAX_BACKGROUND_AGENTS
        from juggle_harness import get_adapter

        agent_cfg = _get_settings().get("agent", {})
        adapter = get_adapter(role, agent_cfg=agent_cfg)
        harness_id = adapter.id

        agents = db.get_all_agents()
        if len(agents) >= MAX_BACKGROUND_AGENTS:
            raise ValueError(
                f"Agent pool full ({MAX_BACKGROUND_AGENTS} max). "
                "Wait for one to finish before spawning more."
            )

        mock_pane = os.environ.get("JUGGLE_TMUX_MOCK_PANE")
        if mock_pane:
            agent_id = db.create_agent(role=role, pane_id=mock_pane, harness=harness_id)
            return db.get_agent(agent_id)

        self.ensure_session()
        pane_id = self.spawn_pane()
        self.start_claude_in_pane(pane_id, model=model, role=role)

        # Detect repo_path at spawn time for get-agent --repo filtering
        try:
            repo_path = subprocess.check_output(
                ["git", "-C", os.getcwd(), "rev-parse", "--show-toplevel"],
                text=True
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            repo_path = ""  # not a git repo

        agent_id = db.create_agent(role=role, pane_id=pane_id, harness=harness_id, repo_path=repo_path)
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


def _get_oneshot_child_pid(pane_id: str) -> int | None:
    """Return the PID of a one-shot child process in *pane_id*, or None.

    Finds the pane's shell PID then looks for a child with JUGGLE_IS_AGENT=1
    in its environment — the same technique ``_pane_has_juggle_agent_env`` uses.
    """
    import subprocess as _sp

    try:
        pane_pid = _sp.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if not pane_pid:
            return None
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
                return int(child)
    except Exception:
        pass
    return None


def oneshot_agent_alive(agent: dict) -> bool:
    """Return True if a one-shot agent process is still running.

    Checks the persisted ``oneshot_pid`` via ``os.kill(pid, 0)``.
    Falls back to ``_pane_has_juggle_agent_env`` when oneshot_pid is not set.
    """
    import os as _os

    pid = agent.get("oneshot_pid")
    if pid is not None:
        try:
            _os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, OSError, ValueError, TypeError):
            return False
    # Fallback: check if the pane still has a child with JUGGLE_IS_AGENT=1
    pane_id = agent.get("pane_id")
    if pane_id:
        return _pane_has_juggle_agent_env(pane_id)
    return False


def reconcile_oneshot_agents(db) -> int:
    """Reconcile stale busy one-shot agents: dead PID → idle + failure action item.

    Only acts on agents whose harness is NON-interactive, status=="busy",
    assigned_thread is not closed/failed, and past a ~20s grace window from
    ``last_send_task_at``.

    Returns the number of agents reconciled.
    """
    import os as _os
    from datetime import datetime, timezone

    from juggle_harness import get_adapter
    from juggle_settings import get_settings as _gs

    reconciled = 0
    now = datetime.now(timezone.utc)
    # Reuse the boot-grace setting for consistency with the watchdog.
    try:
        grace_secs = float(_gs().get("agent_boot_grace_secs", 20))
    except Exception:
        grace_secs = 20

    for agent in db.get_all_agents():
        if agent.get("status") != "busy":
            continue

        harness_id = agent.get("harness")
        if not harness_id:
            continue

        # Resolve interactivity from the agent's PERSISTED harness config, not
        # the current global default. A recycled claude pane must still be
        # treated as interactive even after a config switch to reasonix.
        try:
            agent_cfg = _gs().get("agent", {})
            harnesses = agent_cfg.get("harnesses") or {}
            hcfg = harnesses.get(harness_id)
            if hcfg is not None:
                is_interactive = hcfg.get("interactive", True)
                if is_interactive:
                    continue
            else:
                # Unknown harness — safe default: treat as interactive, skip.
                continue
        except Exception:
            continue

        # Thread already closed/failed → leave untouched (complete/fail-agent
        # already handled it).
        thread_id = agent.get("assigned_thread")
        if thread_id:
            thread = db.get_thread(thread_id)
            if thread and thread.get("status") in ("closed", "failed", "archived"):
                continue

        # Still alive → leave untouched.
        if oneshot_agent_alive(agent):
            continue

        # Within grace window → leave untouched (process may still be spawning).
        last_send_at = agent.get("last_send_task_at")
        if last_send_at:
            try:
                send_dt = datetime.fromisoformat(last_send_at.replace("Z", "+00:00"))
                if send_dt.tzinfo is None:
                    send_dt = send_dt.replace(tzinfo=timezone.utc)
                if (now - send_dt).total_seconds() < grace_secs:
                    continue
            except (ValueError, TypeError):
                pass

        # Dead one-shot with an open thread → set agent idle, file failure.
        label = thread_id[:8] if thread_id else agent["id"][:8]
        if thread_id:
            t = db.get_thread(thread_id)
            if t:
                label = t.get("user_label") or t.get("label") or thread_id[:8]

        db.update_agent(agent["id"], status="idle", assigned_thread=None)
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=(
                    f"⚠️ [{label}] one-shot agent process died without calling "
                    f"complete-agent — investigate and re-dispatch"
                ),
                type_="failure",
                priority="high",
            )
        reconciled += 1

    return reconciled


def _get_pane_start_time(pane_id: str) -> float | None:
    """Return Unix epoch when the pane was created, or None on any failure."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_start_time}"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


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
    cold_start_grace = settings.get("agent_boot_grace_secs", 120)
    current_thread = db.get_current_thread()

    now_ts = datetime.now(timezone.utc)
    reaped = 0

    # DB→tmux: reap DB entries whose panes are gone or past TTL.
    for a in db.get_all_agents():
        # Reap agents whose pane no longer exists, but honour cold-start grace:
        # a freshly-spawned agent whose pane died during Claude boot must not be
        # deleted until agent_boot_grace_secs has elapsed.
        if not mgr.verify_pane(a["pane_id"]):
            created_at = a.get("created_at", "")
            if created_at and cold_start_grace > 0:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now_ts - dt).total_seconds() < cold_start_grace:
                        continue  # still within boot window — skip
                except (ValueError, TypeError):
                    pass
            db.delete_agent(a["id"])
            reaped += 1
            continue

        if a["status"] == "decommission_pending":
            mgr.decommission_agent(db, a["id"])  # kill pane + delete DB record
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
                pane_start = _get_pane_start_time(pane_id)
                if pane_start is None:
                    continue  # conservative: skip if age unreadable
                if time.time() - pane_start < cold_start_grace:
                    continue  # within boot grace — skip
                mgr.kill_pane(pane_id)
                reaped += 1
    except Exception:
        pass

    return reaped
