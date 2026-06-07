#!/usr/bin/env python3
"""
Juggle Hooks - Claude Code hook handlers.
Called automatically by Claude Code on lifecycle events.
Usage: python3 juggle_hooks.py <event_name>
Events: UserPromptSubmit, Stop, SessionStart, PostToolUse
"""

import json
import logging
import re
import subprocess
import os
import sys
import threading
from pathlib import Path

# Add the directory containing this file to sys.path so we can import siblings.
sys.path.insert(0, str(Path(__file__).parent))

from juggle_db import JuggleDB
from juggle_context import build_context_string
from juggle_settings import get_settings as _get_settings

_DATA_DIR = Path(_get_settings()["paths"]["data_dir"]).expanduser()
DB_PATH = _DATA_DIR / "juggle.db"

_CHECKPOINT_PATH = _DATA_DIR / "checkpoint.json"
_CHECKPOINT_MAX_AGE_SECS = 3600  # ignore checkpoints older than 1 h

# Flag file written by /juggle:toggle-autopilot. Its presence means autopilot
# mode is ON. Read here so the directive is re-asserted on every prompt — a
# prompt-only toggle would be forgotten on the next turn.
AUTOPILOT_FLAG = Path.home() / ".juggle" / "autopilot"

logging.basicConfig(
    filename=str(_DATA_DIR / "juggle.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)



def _record_error_safe(exc: Exception, entrypoint: str) -> None:
    """Import record_error lazily to avoid circular import at module load."""
    try:
        from juggle_selfheal import record_error
        record_error(exc, entrypoint)
    except Exception:
        pass  # record_error itself failed; already logged inside


def is_active() -> bool:
    """Return True if juggle is enabled and active."""
    if not DB_PATH.exists():
        return False
    try:
        db = JuggleDB(str(DB_PATH))
        return db.is_active()
    except Exception as exc:
        logging.warning("is_active check failed: %s", exc)
        return False


def get_db() -> JuggleDB:
    return JuggleDB(str(DB_PATH))


# Concise re-assertion of the /juggle:toggle-autopilot loop. The full loop lives
# in commands/toggle-autopilot.md; this is injected every turn so the behavior
# persists once the flag is set, instead of relying on the model remembering it.
_AUTOPILOT_DIRECTIVE = (
    "--- AUTOPILOT MODE: ON ---\n"
    "Autopilot is engaged (~/.juggle/autopilot present). Drive every requested "
    "feature to completion autonomously — do NOT pause for approval:\n"
    "1. Per feature: brainstorm/spec → devil's-advocate critique → resolve open "
    "questions yourself at staff level (decide, note why, proceed).\n"
    "2. Implement on a feature branch via dispatched agents (TDD); verify each "
    "feature with a harness before starting the next.\n"
    "3. Self-unblock: on a blocker or stalled agent, diagnose → recover → continue.\n"
    "4. Escalate ONLY for: missing credentials, an irreversible/destructive "
    "external action, or a product-direction fork with no defensible default.\n"
    "Toggle off with /juggle:toggle-autopilot. See commands/toggle-autopilot.md "
    "for the full loop."
)


def _autopilot_context() -> str:
    """Return the autopilot directive if the flag file is set, else ''."""
    try:
        if AUTOPILOT_FLAG.exists():
            return _AUTOPILOT_DIRECTIVE
    except Exception as exc:
        logging.warning("autopilot flag check failed: %s", exc)
    return ""


def _get_session_id(db) -> str:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT value FROM session WHERE key = 'session_id'"
        ).fetchone()
    return row["value"] if row else ""


_FINANCE_KEYWORDS = re.compile(
    r"\$|dollar|account|routing|payment|tax|irs|refund|balance|transfer|invest|fund|\bira\b|hsa|401k|income|wage|salary|credit|debit",
    re.IGNORECASE,
)
_IDENTITY_KEYWORDS = re.compile(
    r"ssn|license|passport|\bdob\b|date of birth|social security|id number|\bein\b|\btin\b|driver",
    re.IGNORECASE,
)
_ACCOMPLISHMENT_KEYWORDS = re.compile(
    r"\b(filed|completed|finished|done|submitted|launched|shipped|achieved|accomplished)\b",
    re.IGNORECASE,
)
_PREFERENCE_KEYWORDS = re.compile(
    r"\b(prefer|always|never|don't|do not|remember|stop|start|like|dislike|want|need)\b",
    re.IGNORECASE,
)

_CORRECTION_PATTERNS = re.compile(
    r"\b(actually|no it\'s|no,? it\'s|wrong,? it\'s|should be|i meant|to clarify|the correct|it should|correction)\b",
    re.IGNORECASE,
)


def _classify_context(text: str) -> str:
    """Classify text into a Hindsight context tag."""
    if _ACCOMPLISHMENT_KEYWORDS.search(text):
        return "accomplishment"
    if _PREFERENCE_KEYWORDS.search(text):
        return "preference"
    if _IDENTITY_KEYWORDS.search(text):
        return "identity"
    if _FINANCE_KEYWORDS.search(text):
        return "finance"
    return "conversation"


def _retain_conversation_turn(
    role: str, content: str, topic: str, context_override: str | None = None
) -> None:
    """Fire-and-forget: retain a conversation turn to Hindsight."""
    if len(content.strip()) < 20:
        return
    try:
        from juggle_hindsight import HindsightClient

        client = HindsightClient.from_config()
        if client is None:
            return
        text = f"[{topic}] ({role}) {content}"
        if len(text) > 10_000:
            text = text[:10_000]
        context = (
            context_override
            if context_override is not None
            else _classify_context(content)
        )
        client.retain(text, context=context)
    except Exception:
        pass


def get_classification_candidates(threads: list[dict]) -> list[dict]:
    """Return threads eligible for topic classification match.

    Only threads with status not in ('done', 'archived') are considered.
    If no match is found among these candidates, a new thread should be
    created — closed threads are never resurrected.
    """
    return [t for t in threads if t.get("status") not in ("done", "archived")]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# Patterns that are always safe to approve (send "1" = proceed).
# Nothing containing destructive keywords is auto-approved.
_SAFE_APPROVE_PATTERNS = [
    r"Do you want to proceed with",
    r"Do you want to overwrite",
    r"Do you want to create",
    r"Would you like to",
]

# Keywords that indicate a destructive action — never auto-approve if present.
_DESTRUCTIVE_KEYWORDS = [
    "delete",
    "force",
    "reset",
    "remove",
    "drop",
    "destroy",
    "push to main",
    "push to master",
]


def auto_approve_blocked_agents() -> None:
    """Scan busy agent panes and send approval keystrokes for blocked prompts."""
    try:
        db = get_db()
        agents = db.get_all_agents()
        busy_panes = [
            a["pane_id"]
            for a in agents
            if a.get("status") not in ("idle",) and a.get("pane_id")
        ]
        for pane_id in busy_panes:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-20"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                continue
            output = result.stdout

            if "allow Claude to edit its own settings" in output:
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "2", "Enter"],
                    capture_output=True,
                )
                logging.info("auto-approved settings-edit prompt in pane %s", pane_id)
                continue

            output_lower = output.lower()
            if any(kw in output_lower for kw in _DESTRUCTIVE_KEYWORDS):
                logging.info(
                    "skipped auto-approve (destructive keyword) in pane %s", pane_id
                )
                continue

            if any(re.search(p, output, re.IGNORECASE) for p in _SAFE_APPROVE_PATTERNS):
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "1", "Enter"],
                    capture_output=True,
                )
                logging.info("auto-approved prompt in pane %s", pane_id)
    except Exception as exc:
        logging.warning("auto_approve_blocked_agents error: %s", exc)


def handle_user_prompt_submit(data: dict) -> None:
    """Inject juggle context (plus autopilot directive) and record the user prompt."""
    # Agent sessions: inject ONLY the role anchor (build_context_string returns
    # anchor-only when JUGGLE_IS_AGENT=1). Skip the orchestrator dashboard, the
    # autopilot directive, the agent-pane auto-approve sweep, the thread message
    # write, and Hindsight retention — an agent's prompt is a task, not a user
    # turn, so writing it into orchestrator history (and re-injecting ~2000 tokens
    # of dashboard every turn) is pure waste.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        try:
            anchor = build_context_string()
            if anchor:
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": anchor,
                            }
                        }
                    )
                )
        except Exception as exc:
            _record_error_safe(exc, "juggle_hooks.UserPromptSubmit")
            logging.error("UserPromptSubmit agent-anchor error: %s", exc, exc_info=True)
        sys.exit(0)

    autopilot = _autopilot_context()

    # Heartbeat: refresh orchestrator session TTL so a long-running session
    # does not expire mid-work (TTL resets on every prompt from the orchestrator).
    try:
        curr_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if curr_sid and is_active():
            db = get_db()
            if db.get_orchestrator_session_id() == curr_sid:
                db.touch_orchestrator_session_ts()
    except Exception:
        pass

    # Autopilot is independent of juggle mode — re-assert it even when inactive.
    if not is_active():
        if autopilot:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": autopilot,
                        }
                    }
                )
            )
        sys.exit(0)

    auto_approve_blocked_agents()

    try:
        context = build_context_string()
        combined = "\n\n".join(part for part in (autopilot, context) if part)
        if combined:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": combined,
                }
            }
            print(json.dumps(output))

        # Save the user prompt to the messages table for the current thread.
        prompt = data.get("prompt", "")
        if prompt:
            db = get_db()
            thread_id = db.get_current_thread()
            if thread_id is not None:
                db.add_message(thread_id, "user", prompt)
                thread = db.get_thread(thread_id)
                topic = thread.get("topic", "") if thread else ""
                forced_ctx = (
                    "preferences" if _CORRECTION_PATTERNS.search(prompt) else None
                )
                threading.Thread(
                    target=_retain_conversation_turn,
                    args=("user", prompt, topic, forced_ctx),
                    daemon=True,
                ).start()
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.UserPromptSubmit")
        logging.error("UserPromptSubmit handler error: %s", exc, exc_info=True)

    sys.exit(0)


# Patterns indicating the orchestrator asked for permission instead of acting.
_PERMISSION_ASKING_PATTERNS = [
    r"should i (proceed|dispatch|implement|go ahead|make|fix|run)",
    r"want me to (implement|dispatch|fix|proceed|run|make)",
    r"shall i (implement|dispatch|proceed|fix)",
    r"dispatch the coder\??",
    r"ready to implement\??",
    r"do you want me to",
]


def handle_stop(data: dict) -> None:
    """Capture last assistant message and mark notifications delivered."""
    if not is_active():
        sys.exit(0)

    try:
        db = get_db()

        # Capture orchestrator response — available via last_assistant_message field
        # (added to Stop hook payload in recent Claude Code release)
        last_msg = data.get("last_assistant_message", "").strip()
        if last_msg and len(last_msg) > 10:
            thread_id = db.get_current_thread()
            if thread_id is not None:
                db.add_message(thread_id, "assistant", last_msg)
                logging.info(
                    "Stop: captured assistant message for thread %s (%d chars)",
                    thread_id,
                    len(last_msg),
                )

                # Auto-retain assistant response to Hindsight
                thread = db.get_thread(thread_id)
                topic = thread.get("topic", "") if thread else ""
                threading.Thread(
                    target=_retain_conversation_turn,
                    args=("assistant", last_msg, topic),
                    daemon=True,
                ).start()

                # Violation: orchestrator asked for permission instead of acting
                if any(
                    re.search(p, last_msg, re.IGNORECASE)
                    for p in _PERMISSION_ASKING_PATTERNS
                ):
                    db.add_notification_v2(
                        thread_id,
                        "⚠️ ORCHESTRATOR: You asked for permission instead of acting. "
                        "Clear fixes → dispatch immediately. Only gate on genuine design "
                        "decisions via AskUserQuestion.",
                        session_id=_get_session_id(db),
                    )
                    logging.warning(
                        "Stop: permission-asking detected in thread %s", thread_id
                    )
        # Class B: scan transcript for Juggle-caused tool errors
        _scan_transcript_for_class_b(data)
        # Clean up pre-compaction checkpoint on normal session end.
        try:
            _CHECKPOINT_PATH.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.Stop")
        logging.error("Stop handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_session_start(data: dict) -> None:
    """Inject restoration context on resume or compact."""
    # Agent sessions never need the orchestrator's startup dashboard (topics tree
    # + Hindsight recalls). Without this guard every dispatched agent effectively
    # "calls /juggle:start" at boot, paying for context it is told to ignore.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        sys.exit(0)

    if not is_active():
        db = get_db()
        if db.get_threads_by_status("open"):
            db.set_active(True)
        else:
            sys.exit(0)

    try:
        reason = data.get("reason", "")
        # Inject context for resume/compact or when reason is unknown/absent.
        if reason not in ("new",):
            db = get_db()
            from juggle_context import build_startup_output

            additional_context = build_startup_output(db)
            # Append self-heal pending count
            try:
                from juggle_selfheal import _get_pending_selfheal_count
                pending = _get_pending_selfheal_count(db)
                if pending > 0:
                    additional_context += (
                        f"\n\u26a0\ufe0f {pending} pending self-heal error(s) \u2014 "
                        "run `list-selfheal` to review."
                    )
            except Exception:
                pass
            # Restore pre-compaction state if a fresh checkpoint exists.
            try:
                additional_context += _restore_checkpoint(db)
            except Exception:
                pass
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context,
                }
            }
            print(json.dumps(output))
        # Reap checkpoints older than 24 h regardless of reason.
        try:
            import time as _t
            if _CHECKPOINT_PATH.exists():
                cp = json.loads(_CHECKPOINT_PATH.read_text())
                if _t.time() - cp.get("ts", 0) > 86400:
                    _CHECKPOINT_PATH.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.SessionStart")
        logging.error("SessionStart handler error: %s", exc, exc_info=True)

    sys.exit(0)


def _write_checkpoint(db) -> None:
    """Atomically write orchestrator state snapshot to checkpoint.json."""
    import time

    session_id = _get_session_id(db)
    thread_id = db.get_current_thread()
    thread_label = None
    if thread_id:
        thread = db.get_thread(thread_id)
        thread_label = thread.get("user_label") if thread else None

    busy = [a for a in db.get_all_agents() if a.get("status") == "busy"]
    in_flight = [
        {
            "agent_id": a["id"],
            "thread_id": a.get("assigned_thread"),
            "dispatched_at": a.get("created_at"),
        }
        for a in busy
    ]

    with db._connect() as conn:
        row = conn.execute("SELECT MAX(id) as m FROM notifications_v2").fetchone()
        cursor = row["m"] if row and row["m"] is not None else 0

    action_items = db.get_open_action_items()
    pending_head = action_items[0]["id"] if action_items else None

    payload = {
        "ts": time.time(),
        "session_id": session_id,
        "active_thread_id": thread_id,
        "active_thread_label": thread_label,
        "in_flight_dispatches": in_flight,
        "notification_cursor": cursor,
        "pending_action_item_head": pending_head,
    }
    tmp = _CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, _CHECKPOINT_PATH)


def _restore_checkpoint(db) -> str:
    """Return an additionalContext string from the checkpoint, or '' if absent/stale."""
    import time

    if not _CHECKPOINT_PATH.exists():
        return ""
    try:
        cp = json.loads(_CHECKPOINT_PATH.read_text())
    except Exception:
        return ""
    age = time.time() - cp.get("ts", 0)
    if age >= _CHECKPOINT_MAX_AGE_SECS:
        return ""
    if cp.get("session_id") != _get_session_id(db):
        return ""

    label = cp.get("active_thread_label") or "?"
    tid = (cp.get("active_thread_id") or "")[:8]
    in_flight = cp.get("in_flight_dispatches", [])
    cursor = cp.get("notification_cursor", 0)
    parts = [f"active=[{label}] {tid}"]
    if in_flight:
        parts.append(f"{len(in_flight)} agent(s) in flight")
    parts.append(f"notification cursor {cursor}")
    return f"\n\n⟳ Resuming after compaction: {', '.join(parts)}."


def handle_pre_compact(data: dict) -> None:
    """Write checkpoint before compaction so SessionStart can restore state."""
    if os.environ.get("JUGGLE_IS_AGENT"):
        sys.exit(0)
    if not is_active():
        sys.exit(0)
    try:
        db = get_db()
        _write_checkpoint(db)
        logging.info("PreCompact: checkpoint written to %s", _CHECKPOINT_PATH)
        print(json.dumps({"systemMessage": "Checkpointed orchestrator state for compaction"}))
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PreCompact")
        logging.error("PreCompact handler error: %s", exc, exc_info=True)
    sys.exit(0)


# Patterns that indicate a Bash command writes or deletes files.
# Each entry: (compiled regex, human-readable label).
_BASH_WRITE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsed\s+(-[^\s]*i[^\s]*\s|--in-place)"), "sed -i (in-place edit)"),
    (
        re.compile(r"\bgit\s+(add|commit|push|reset|rebase|merge|rm|mv)\b"),
        "git write operation",
    ),
    (re.compile(r"\brm\s+"), "rm (file deletion)"),
    (re.compile(r"\bpatch\b"), "patch"),
    (re.compile(r"\btee\s+\S"), "tee (file write)"),
    (re.compile(r"\bmv\s+\S+\s+\S"), "mv (file move/overwrite)"),
    # Redirect to file: `>` or `>>` not preceded by a digit/& (fd redirects like 2>, &>)
    # and not targeting /dev/* or a file descriptor (&1, &2).
    # Lookahead includes \s* to prevent backtracking from defeating the /dev/ exclusion.
    (
        re.compile(r"(?<![0-9&|])\s*>{1,2}(?!\s*(?:/dev/|/tmp/|&|\d))"),
        "output redirect (> or >>)",
    ),
]

_BASH_WRITE_ALLOW_PATTERNS: list[re.Pattern] = [
    re.compile(r"/tmp/juggle_"),  # juggle orchestrator task-file writes
]


def _bash_write_pattern(command: str) -> str | None:
    """Return the label of the first file-write pattern found, or None."""
    for allow_pat in _BASH_WRITE_ALLOW_PATTERNS:
        if allow_pat.search(command):
            return None
    for pattern, label in _BASH_WRITE_PATTERNS:
        if pattern.search(command):
            return label
    return None


def _tool_input_sample(tool_input) -> str | None:
    """Return a short (<=120 char) representative sample of a tool's input.

    Used only for human-readable context in the usage report — not parsed. Picks
    the most telling field per tool (Bash command, file path, query) and
    truncates, so the telemetry row never stores large payloads.
    """
    if not isinstance(tool_input, dict) or not tool_input:
        return None
    for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            sample = val if len(val) <= 120 else val[:117] + "..."
            return f"{key}={sample}"
    # Fall back to the first key name so the report still hints at shape.
    return next(iter(tool_input), None)


def _log_agent_tool_use(data: dict) -> None:
    """Best-effort: record this agent's tool call for usage analytics.

    Runs out-of-band in the hook subprocess, so it adds ZERO tokens to the
    agent's context. Must never raise — a telemetry failure must not block the
    agent's tool call.
    """
    try:
        tool_name = data.get("tool_name", "")
        if not tool_name:
            return
        role = os.environ.get("JUGGLE_AGENT_ROLE") or "unknown"
        mode = "audit" if os.environ.get("JUGGLE_AGENT_AUDIT") else "normal"
        sample = _tool_input_sample(data.get("tool_input"))
        JuggleDB(str(DB_PATH)).record_agent_tool_use(role, tool_name, mode, sample)
    except Exception as exc:  # telemetry is never allowed to break the agent
        logging.warning("agent tool-use logging failed: %s", exc)


_ORCHESTRATOR_SESSION_TTL_SECS = 86400  # 24 hours


def _is_orchestrator_session(data: dict) -> bool:
    """Return True iff the current session is the registered orchestrator and not stale.

    Guards handle_pre_tool_use: only block edits in the exact session that ran
    /juggle:start, never in other active Claude Code sessions.

    Rules:
    - orchestrator_session_id not set → False (safe default: allow all)
    - current session_id != orchestrator_session_id → False (different session)
    - registration timestamp older than TTL → False (stale; clear and allow)
    - otherwise → True (block)
    """
    import time

    try:
        db = get_db()
        orch_sid = db.get_orchestrator_session_id()
        if not orch_sid:
            return False  # no orchestrator registered → allow all sessions

        curr_sid = data.get("session_id", "")
        if curr_sid != orch_sid:
            return False  # different session → not the orchestrator

        # TTL check: stale orchestrator session should not haunt new sessions
        ts = db.get_orchestrator_session_ts()
        if ts and (time.time() - ts) > _ORCHESTRATOR_SESSION_TTL_SECS:
            db.set_orchestrator_session_id("")  # expire the stale registration
            logging.info("PreToolUse: orchestrator session %s expired (>24h)", orch_sid[:8])
            return False

        return True
    except Exception as exc:
        logging.warning("_is_orchestrator_session check failed: %s", exc)
        return False  # on error, fail open (allow) to avoid false positives


def handle_pre_tool_use(data: dict) -> None:
    """Hard-block Edit/Write/NotebookEdit/Bash-writes in the orchestrator main thread.

    Agent sessions bypass the orchestrator guard via JUGGLE_IS_AGENT=1 (set by
    start_claude_in_pane), but first log the tool call for usage analytics.
    Trust model: JUGGLE_IS_AGENT is an anti-accidental-edit guard, not a hard
    security boundary — see juggle_tmux.py for details.
    """
    # Agent panes are explicitly tagged — record what they use, then let them
    # write freely (no orchestrator blocking applies to agents).
    if os.environ.get("JUGGLE_IS_AGENT"):
        _log_agent_tool_use(data)
        sys.exit(0)

    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")
        BLOCKED_TOOLS = {"Edit", "Write", "NotebookEdit"}
        _TMP_PREFIXES = ("/tmp/", "/private/tmp/")
        # Blocking only applies to the exact session that activated orchestrator mode.
        # All other sessions (incl. non-juggle windows) are allowed through.
        if tool_name in BLOCKED_TOOLS and _is_orchestrator_session(data):
            file_path = data.get("tool_input", {}).get("file_path", "")
            if tool_name in ("Write", "Edit") and any(
                file_path.startswith(p) for p in _TMP_PREFIXES
            ):
                sys.exit(0)
            session_id = data.get("session_id", "")[:8]
            msg = (
                f"🚫 {tool_name} blocked in juggle orchestrator session [{session_id}]. "
                "File edits must go through an agent. Use get-agent + send-task to dispatch."
            )
            logging.info(
                "PreToolUse: blocked %s in orchestrator session %s",
                tool_name,
                session_id,
            )
            output = {
                "hookSpecificOutput": {"permissionDecision": "deny"},
                "systemMessage": msg,
            }
            print(json.dumps(output), file=sys.stderr)
            sys.exit(2)
        if tool_name == "Bash" and _is_orchestrator_session(data):
            command = data.get("tool_input", {}).get("command", "")
            matched = _bash_write_pattern(command)
            if matched:
                session_id = data.get("session_id", "")[:8]
                msg = (
                    f"🚫 Bash blocked in juggle orchestrator session [{session_id}]: "
                    f"detected file-write pattern '{matched}'. "
                    "File modifications must go through an agent. Use get-agent + send-task to dispatch."
                )
                logging.info(
                    "PreToolUse: blocked Bash(%s) in orchestrator session %s",
                    matched,
                    session_id,
                )
                output = {
                    "hookSpecificOutput": {"permissionDecision": "deny"},
                    "systemMessage": msg,
                }
                print(json.dumps(output), file=sys.stderr)
                sys.exit(2)
        # Track pending AskUserQuestion decisions
        if tool_name == "AskUserQuestion":
            try:
                db = get_db()
                thread_id = db.get_current_thread()
                if thread_id:
                    tool_use_id = data.get("tool_use_id", "")
                    questions = data.get("tool_input", {}).get("questions", [])

                    thread = db.get_thread(thread_id)
                    current = thread.get("open_questions") or []
                    if isinstance(current, str):
                        current = json.loads(current)

                    for i, q in enumerate(questions):
                        current.append(
                            {
                                "id": f"{tool_use_id}:{i}",
                                "text": q.get("question", ""),
                                "source": "askuser",
                            }
                        )

                    db.update_thread(thread_id, open_questions=current)

                    # Create cockpit action item for this decision
                    question_text = " / ".join(q.get("question", "") for q in questions)
                    db.add_action_item(
                        thread_id=thread_id,
                        message=f"[tuid:{tool_use_id}] Decision needed: {question_text}",
                        type_="decision",
                        priority="normal",
                    )
            except Exception as exc:
                logging.warning("AskUserQuestion PreToolUse handler error: %s", exc)

    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PreToolUse")
        logging.error("PreToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_post_tool_use(data: dict) -> None:
    """Detect orchestrator violations and JUGGLE ACTIVE leaks in tool calls."""
    # Agent sessions: none of the orchestrator-violation logic applies. Agents
    # are SUPPOSED to Read/Grep/Glob and they never call Agent. Without this
    # guard every file read in an agent injects a ~40-word "ORCHESTRATOR
    # VIOLATION" warning into the agent's context — a per-tool-call token leak
    # and a false accusation. Exit before any check.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        sys.exit(0)

    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")

        # Warning-only for search/read tools (sometimes legitimate for quick lookups)
        WARNING_TOOLS = {"Read", "Glob", "Grep", "WebFetch", "WebSearch"}
        if tool_name in WARNING_TOOLS:
            warning = (
                f"⚠️ ORCHESTRATOR VIOLATION: You used [{tool_name}] directly in the main thread. "
                "All file reads and searches MUST go through tmux agents. "
                "Dispatch a task to a tmux agent instead."
            )
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": warning,
                }
            }
            print(json.dumps(output))
            sys.exit(0)

        # Clear pending decisions after AskUserQuestion completes
        if tool_name == "AskUserQuestion":
            try:
                db = get_db()
                thread_id = db.get_current_thread()
                if thread_id:
                    tool_use_id = data.get("tool_use_id", "")

                    thread = db.get_thread(thread_id)
                    open_questions = thread.get("open_questions") or []
                    if isinstance(open_questions, str):
                        open_questions = json.loads(open_questions)

                    open_questions = [
                        q
                        for q in open_questions
                        if not q.get("id", "").startswith(tool_use_id)
                    ]

                    db.update_thread(thread_id, open_questions=open_questions)

                    # Dismiss cockpit action item for this decision
                    prefix = f"[tuid:{tool_use_id}]"
                    open_items = db.get_open_action_items()
                    for item in open_items:
                        if item.get("message", "").startswith(prefix):
                            db.dismiss_action_item(item["id"])
            except Exception as exc:
                logging.warning("AskUserQuestion PostToolUse handler error: %s", exc)
            sys.exit(0)

        if tool_name != "Agent":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        if not isinstance(tool_input, dict):
            sys.exit(0)

        # Violation: foreground Agent call (run_in_background not set or false)
        if not tool_input.get("run_in_background"):
            warning = (
                "⚠️ ORCHESTRATOR VIOLATION: Foreground Agent call detected. "
                "All Agent calls MUST use run_in_background=true. The orchestrator must stay responsive."
            )
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": warning,
                }
            }
            print(json.dumps(output))
            sys.exit(0)

        # Extract JUGGLE_THREAD:<id> from the agent prompt.
        prompt = tool_input.get("prompt", "")
        thread_match = re.search(r"\[JUGGLE_THREAD:([^\]]+)\]", prompt)
        thread_id = thread_match.group(1) if thread_match else None

        # Violation logging: warn if JUGGLE ACTIVE block was forwarded to a sub-agent.
        if "JUGGLE ACTIVE" in prompt:
            db = get_db()
            warning = (
                f"[juggle] WARNING: Agent prompt for thread {thread_id or '?'} contains "
                f"'JUGGLE ACTIVE' block — context leaked to sub-agent. "
                f"Strip JUGGLE blocks before dispatching agents."
            )
            current = db.get_current_thread()
            db.add_notification_v2(
                thread_id or current or "", warning, session_id=_get_session_id(db)
            )
            logging.warning(
                "JUGGLE ACTIVE leaked into sub-agent prompt for thread %s", thread_id
            )

    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PostToolUse")
        logging.error("PostToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)



# ---------------------------------------------------------------------------
# Class B: Stop-hook transcript scan (corrected real JSONL schema)
# ---------------------------------------------------------------------------

_JUGGLE_PATHS: tuple[str, ...] = (
    "juggle_cli.py",
    "juggle_hooks.py",
    "juggle_selfheal.py",
    "scripts/juggle-",
    "commands/",
    "juggle:",
)

_MAX_TRANSCRIPT_LINES = 200


def _scan_transcript_for_class_b(data: dict) -> None:
    """Called from handle_stop(). Silently skips if no transcript_path."""
    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return
    try:
        _do_class_b_scan(Path(transcript_path))
    except Exception as exc:
        logging.warning("Class B transcript scan failed: %s", exc)


def _do_class_b_scan(transcript_path: Path) -> None:
    """Parse transcript JSONL and record tool errors attributed to Juggle.

    Verified real schema (2026-05-30):
    - type="user" with message.content=str → human turn boundary
    - type="assistant" → tool_use blocks in message.content list
    - type="user" with message.content=list → tool_result blocks
    - tool_use: {type, id, name, input, caller}
    - tool_result: {type, tool_use_id, is_error, content}
    - is_error is True for errors; False or None for success
    """
    all_lines = transcript_path.read_text(errors="replace").splitlines()
    lines = all_lines[-_MAX_TRANSCRIPT_LINES:]

    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Find last human-text turn boundary
    boundary_idx = -1
    for i, rec in enumerate(records):
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", "")
        if isinstance(content, str):
            boundary_idx = i
        elif isinstance(content, list):
            if any(isinstance(x, dict) and x.get("type") == "text" for x in content):
                boundary_idx = i

    if boundary_idx < 0:
        return

    current_turn = records[boundary_idx + 1:]

    tool_uses: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_uses.append(item)

    tool_results: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_results.append(item)

    _attribute_tool_errors(tool_uses, tool_results)


def _attribute_tool_errors(tool_uses: list[dict], tool_results: list[dict]) -> None:
    """N=10 same-turn causal attribution."""
    from juggle_selfheal import record_orchestration_error

    N = 10
    recent_uses = tool_uses[-N:]
    recent_inputs_str = " ".join(json.dumps(tc.get("input") or {}) for tc in recent_uses)

    juggle_ref: str | None = None
    for path in _JUGGLE_PATHS:
        if path in recent_inputs_str:
            juggle_ref = path
            break

    if juggle_ref is None:
        return

    use_by_id = {tc.get("id"): tc for tc in tool_uses}

    for tr in tool_results:
        if tr.get("is_error") is not True:
            continue
        error_text = str(tr.get("content", ""))
        use_id = tr.get("tool_use_id")
        tc = use_by_id.get(use_id, {})
        tool_name = tc.get("name", "unknown")
        tool_input = tc.get("input") or {}
        record_orchestration_error(tool_name, tool_input, error_text, juggle_ref)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HANDLERS = {
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "SessionStart": handle_session_start,
    "PreCompact": handle_pre_compact,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
}


def main() -> None:
    if len(sys.argv) < 2:
        logging.error("juggle_hooks.py called without an event name argument")
        sys.exit(0)

    event_name = sys.argv[1]

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        logging.error("Failed to parse stdin JSON for event %s: %s", event_name, exc)
        data = {}

    handler = HANDLERS.get(event_name)
    if handler is None:
        logging.warning("Unknown hook event: %s", event_name)
        sys.exit(0)

    try:
        handler(data)
    except Exception as exc:
        _record_error_safe(exc, f"juggle_hooks.{event_name}")
        logging.error("Unhandled error in hook %s: %s", event_name, exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
