"""Research-agent execution paths for the /schedule:dogfood routine.

Extracted from schedules.dogfood (loc-gate budget, 2026-08-02): Path B (headless
`claude -p`), Path A (the Juggle tmux CLI, which falls back to Path B at every
failure point), and the tmux probe that chooses between them. Pure move, no
behaviour change — the two duplicated function-local `import json` statements are
hoisted to module scope.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from schedules.common import (
    JUGGLE_REPO,
    CostTracker,
    db_query,
    get_db,
    today_str,
)

logger = logging.getLogger(__name__)

AGENT_TIMEOUT_SECS = 600  # 10 min


def _tmux_session_exists(session: str = "juggle") -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True, text=True
    )
    return result.returncode == 0


def _run_headless_research(task_prompt: str, cost_tracker: CostTracker, dry_run: bool) -> str:
    """Run research via claude -p (Path B — headless, no tmux required)."""
    if dry_run:
        return (
            "## Observed Friction Patterns\n"
            "1. [DRY RUN] Simulated friction pattern: agents frequently stall on tool-use confirmation.\n\n"
            "## Repeated Dispatches / Blockers\n"
            "No repeated dispatches detected in dry run.\n\n"
            "## Unresolved Open Questions\n"
            "None in dry run.\n\n"
            "## Suggested Improvements (1–3)\n"
            "1. **[DRY RUN] Reduce confirmation prompts** — add more auto-approved tool patterns "
            "in `src/juggle_hooks.py:45`. See settings.json `permissions.allow`.\n\n"
            "## Raw thread summary (for archival)\n"
            "[DRY RUN] No live DB query performed.\n"
        )

    model = "claude-sonnet-4-6"
    try:
        result = subprocess.run(
            ["claude", "-p", task_prompt, "--model", model, "--output-format", "json"],
            capture_output=True, text=True, timeout=AGENT_TIMEOUT_SECS
        )
        if result.returncode != 0:
            logger.warning("claude -p failed rc=%d: %s", result.returncode, result.stderr[:200])
            return ""
        try:
            data = json.loads(result.stdout)
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cost = cost_tracker.estimate_from_tokens(in_tok, out_tok, model)
            cost_tracker.add(cost)
            if isinstance(data, dict):
                return data.get("result", data.get("content", result.stdout))
        except Exception:
            pass
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.error("dogfood research agent timed out after %ds", AGENT_TIMEOUT_SECS)
        raise


def _run_juggle_path_a(task_prompt: str, cost_tracker: CostTracker) -> str:
    """Run research via Juggle CLI (Path A — tmux session exists)."""
    cli = str(JUGGLE_REPO / "src" / "juggle_cli.py")
    today = today_str()
    topic = f"dogfood-{today}"

    # Create thread
    result = subprocess.run(
        [sys.executable, cli, "create-thread", topic],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        logger.warning("create-thread failed, falling back to headless: %s", result.stderr)
        return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

    # Write task to temp file and send
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(task_prompt)
        task_file = f.name

    try:
        # Get a researcher agent
        agent_result = subprocess.run(
            [sys.executable, cli, "get-agent", "--role", "researcher"],
            capture_output=True, text=True, timeout=60
        )
        if agent_result.returncode != 0:
            logger.warning("get-agent failed, falling back to headless")
            return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

        agent_id = agent_result.stdout.strip()
        if not agent_id:
            return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

        subprocess.run(
            [sys.executable, cli, "send-task", agent_id, task_file],
            capture_output=True, text=True, timeout=30
        )

        # Poll for completion (up to AGENT_TIMEOUT_SECS)
        deadline = time.time() + AGENT_TIMEOUT_SECS
        while time.time() < deadline:
            time.sleep(15)
            check = subprocess.run(
                [sys.executable, cli, "check-agents"],
                capture_output=True, text=True, timeout=10
            )
            if check.returncode == 0:
                try:
                    agents = json.loads(check.stdout or "[]")
                    this_agent = next((a for a in agents if a.get("id") == agent_id), None)
                    if this_agent and this_agent.get("status") in ("idle", "completed"):
                        break
                except Exception:
                    pass
        else:
            logger.warning("dogfood agent did not complete within timeout")

        # Retrieve completion summary
        rows = db_query(get_db(), "SELECT result_summary FROM agent_completions ORDER BY id DESC LIMIT 1")
        return rows[0].get("result_summary", "") if rows else ""

    finally:
        Path(task_file).unlink(missing_ok=True)
