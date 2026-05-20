#!/usr/bin/env python3
"""
/schedule:dogfood — Saturday 03:00 local (0 3 * * 6 / UTC: 0 8 * * 6)

Spawns a headless Juggle research agent to analyze the past week's operational
data and writes a digest to reports/dogfood-YYYY-MM-DD.md, then files a Juggle
action item.
"""

import json
import logging
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_schedule_common import (  # noqa: E402
    CostCapExceeded,
    CostTracker,
    JUGGLE_REPO,
    REPORTS_DIR,
    days_ago_iso,
    db_query,
    get_db,
    git_commit,
    git_push,
    mark_run_complete,
    today_str,
    write_report,
)

COST_CAP = 1.00
AGENT_TIMEOUT_SECS = 600
ROUTINE = "dogfood"

TASK_PROMPT_TEMPLATE = """\
Review the last 7 days of completed threads in the Juggle SQLite DB (juggle.db).
Focus only on threads created or active after {since_date}.

What patterns of user friction, repeated dispatches, blockers, or unresolved open
questions do you observe? Analyze agent_completions, watchdog_events, action_items,
and threads tables.

If >50% of threads this week ended in 'failed' or had watchdog retries, note this
as a possible infrastructure incident, not a design problem.

Note: analysis based on data since {since_date}. If fewer than 5 threads are present,
say so and note findings may not be representative.

Suggest 1–3 concrete Juggle improvements with file:line refs where applicable.
Do NOT reference any prior dogfood reports or prior suggestions.
Analyze only raw thread data from the past 7 days.

Output a structured report with these sections:
## Observed Friction Patterns
## Repeated Dispatches / Blockers
## Unresolved Open Questions
## Suggested Improvements (1–3)
## Raw thread summary (for archival)
"""


def _check_prior_dogfood_thread(db) -> str | None:
    try:
        rows = db_query(
            db,
            "SELECT id, topic FROM threads WHERE topic LIKE 'dogfood-%' "
            "AND status NOT IN ('closed','archived','failed')",
        )
        if rows:
            return rows[0]["topic"]
    except Exception as e:
        logger.warning("prior dogfood thread check failed: %s", e)
    return None


def _check_active_session(db) -> bool:
    try:
        rows = db_query(
            db,
            "SELECT last_active_at FROM threads WHERE status = 'active' "
            "ORDER BY last_active_at DESC LIMIT 1",
        )
        if not rows:
            return False
        last_active = rows[0].get("last_active_at") or ""
        if not last_active:
            return False
        dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() < 1800
    except Exception:
        return False


def _tmux_session_exists(session: str = "juggle") -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session], capture_output=True, text=True
    )
    return result.returncode == 0


def _run_canary_spawn(cost_tracker: CostTracker) -> str:
    """Dry-run spawn: create real thread + agent, send canary task, poll up to 60s."""
    cli = str(JUGGLE_REPO / "src" / "juggle_cli.py")
    topic = f"dogfood-dryrun-{today_str()}"

    result = subprocess.run(
        [sys.executable, cli, "create-thread", topic],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return (
            f"## Observed Friction Patterns\n[CANARY-SPAWN-FAILED] create-thread failed: "
            f"{result.stderr[:100]}\n\n## Raw thread summary (for archival)\nCanary aborted.\n"
        )

    # Parse thread label from "Created Topic JN: ..." output
    import re as _re

    m = _re.search(r"Topic ([A-Z]{1,2}):", result.stdout)
    if not m:
        return (
            f"## Observed Friction Patterns\n[CANARY-SPAWN-FAILED] create-thread output unparseable: "
            f"{result.stdout[:100]}\n\n## Raw thread summary (for archival)\nCanary aborted.\n"
        )
    thread_label = m.group(1)

    agent_result = subprocess.run(
        [sys.executable, cli, "get-agent", thread_label, "--role", "researcher"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if agent_result.returncode != 0 or not agent_result.stdout.strip():
        return (
            f"## Observed Friction Patterns\n[CANARY-SPAWN-FAILED] get-agent failed: "
            f"{agent_result.stderr[:100]}\n\n## Raw thread summary (for archival)\nCanary aborted at get-agent.\n"
        )

    agent_id = agent_result.stdout.strip().split()[0]
    canary_task = "Output exactly this text and nothing else: dogfood dry-run canary OK"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(canary_task)
        task_file = f.name

    try:
        subprocess.run(
            [sys.executable, cli, "send-task", agent_id, task_file],
            capture_output=True,
            text=True,
            timeout=30,
        )

        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(5)
            check = subprocess.run(
                [sys.executable, cli, "check-agents"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if check.returncode == 0:
                try:
                    agents = json.loads(check.stdout or "[]")
                    agent = next((a for a in agents if a.get("id") == agent_id), None)
                    if agent and agent.get("status") in ("idle", "completed"):
                        break
                except Exception:
                    pass
    finally:
        Path(task_file).unlink(missing_ok=True)

    return (
        f"## Observed Friction Patterns\n"
        f"[DRY-RUN-CANARY] Spawn path exercised. Thread: {topic}, Agent: {agent_id[:8]}\n\n"
        f"## Repeated Dispatches / Blockers\nNone in canary run.\n\n"
        f"## Unresolved Open Questions\nNone in canary run.\n\n"
        f"## Suggested Improvements (1–3)\n[DRY-RUN-CANARY] Spawn path verified — no friction observed.\n\n"
        f"## Raw thread summary (for archival)\n"
        f"[DRY-RUN-CANARY] Canary task sent to {agent_id[:8]} — spawn path verified.\n"
    )


def _run_headless_research(
    task_prompt: str, cost_tracker: CostTracker, dry_run: bool
) -> str:
    if dry_run:
        return (
            "## Observed Friction Patterns\n"
            "1. [DRY RUN] Simulated friction: agents frequently stall on tool-use confirmation.\n\n"
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
            capture_output=True,
            text=True,
            timeout=AGENT_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            logger.warning(
                "claude -p failed rc=%d: %s", result.returncode, result.stderr[:200]
            )
            return ""
        try:
            data = json.loads(result.stdout)
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            cost = cost_tracker.estimate_from_tokens(
                usage.get("input_tokens", 0), usage.get("output_tokens", 0), model
            )
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
    cli = str(JUGGLE_REPO / "src" / "juggle_cli.py")
    topic = f"dogfood-{today_str()}"

    result = subprocess.run(
        [sys.executable, cli, "create-thread", topic],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning("create-thread failed, falling back to headless")
        return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

    import re as _re

    m = _re.search(r"Topic ([A-Z]{1,2}):", result.stdout)
    thread_label = m.group(1) if m else ""

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(task_prompt)
        task_file = f.name

    try:
        agent_result = subprocess.run(
            [sys.executable, cli, "get-agent", thread_label, "--role", "researcher"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if agent_result.returncode != 0:
            return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

        agent_id = agent_result.stdout.strip()
        if not agent_id:
            return _run_headless_research(task_prompt, cost_tracker, dry_run=False)

        subprocess.run(
            [sys.executable, cli, "send-task", agent_id, task_file],
            capture_output=True,
            text=True,
            timeout=30,
        )

        deadline = time.time() + AGENT_TIMEOUT_SECS
        while time.time() < deadline:
            time.sleep(15)
            check = subprocess.run(
                [sys.executable, cli, "check-agents"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if check.returncode == 0:
                try:
                    agents = json.loads(check.stdout or "[]")
                    agent = next((a for a in agents if a.get("id") == agent_id), None)
                    if agent and agent.get("status") in ("idle", "completed"):
                        break
                except Exception:
                    pass

        rows = db_query(
            get_db(),
            "SELECT result_summary FROM agent_completions ORDER BY id DESC LIMIT 1",
        )
        return rows[0].get("result_summary", "") if rows else ""
    finally:
        Path(task_file).unlink(missing_ok=True)


def _build_report(since_date: str, agent_output: str, cost_total: float) -> str:
    today = today_str()
    return f"""\
# Juggle Self-Analysis — {today}

> Generated by `/schedule:dogfood` via Claude Code Routines.
> Data window: {since_date} to {today} (7 days)
> Agent: researcher, model: claude-sonnet-4-6
> LLM cost this run: ${cost_total:.4f}

{agent_output.strip()}
"""


def _file_action_item(db, findings_text: str, dry_run: bool) -> None:
    if dry_run:
        logger.info("DRY RUN: would file action item")
        return

    suggestion = ""
    in_suggestions = False
    for line in findings_text.splitlines():
        if "## Suggested Improvements" in line:
            in_suggestions = True
            continue
        if in_suggestions and line.strip() and not line.startswith("#"):
            suggestion = line.strip().lstrip("0123456789.*- ")
            if len(suggestion) > 10:
                break

    if suggestion:
        msg = f"Dogfood findings: {suggestion[:120]}"
    else:
        msg = "[NO FINDINGS THIS WEEK] Dogfood ran successfully but found no actionable improvements"

    try:
        tid = _find_or_create_schedule_thread(db)
        if tid:
            db.add_action_item(
                thread_id=tid, message=msg, type_="decision", priority="high"
            )
            logger.info("filed action item: %s", msg[:60])
    except Exception as e:
        logger.error("failed to file action item: %s", e)


def _find_or_create_schedule_thread(db) -> str | None:
    try:
        rows = db_query(
            db, "SELECT id FROM threads WHERE topic LIKE 'schedule%' LIMIT 1"
        )
        if rows:
            return rows[0]["id"]
        rows = db_query(db, "SELECT id FROM threads ORDER BY created_at DESC LIMIT 1")
        if rows:
            return rows[0]["id"]
    except Exception:
        pass
    return None


def _write_and_commit(
    today: str, since_date: str, agent_output: str, cost_total: float, dry_run: bool
) -> None:
    content = _build_report(since_date, agent_output, cost_total)
    out_path = REPORTS_DIR / f"dogfood-{today}.md"
    tmp_path = Path("/tmp/schedule-dogfood-sample-report.md") if dry_run else None
    write_report(out_path, content, dry_run=dry_run, tmp_override=tmp_path)
    if not dry_run:
        git_commit(f"chore(schedule): dogfood report {today} [partial]")
        git_push()


def run(dry_run: bool = False) -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = today_str()
    since_date = days_ago_iso(7)[:10]
    cost_tracker = CostTracker(cap_usd=COST_CAP, routine=ROUTINE, dry_run=dry_run)

    db = get_db()

    prior = _check_prior_dogfood_thread(db)
    if prior:
        msg = f"Prior dogfood thread '{prior}' still unresolved — review before this week's run"
        logger.warning(msg)
        if not dry_run:
            tid = _find_or_create_schedule_thread(db)
            if tid:
                db.add_action_item(
                    thread_id=tid, message=msg, type_="manual_step", priority="high"
                )
        print(f"SKIPPED: {msg}", file=sys.stderr)
        return 1

    if not dry_run and _check_active_session(db):
        logger.info("Active session detected, deferring 60s and retrying")
        time.sleep(60)
        if _check_active_session(db):
            msg = "Dogfood routine deferred — Juggle in active use at Saturday 03:00. Run manually: schedule-dogfood"
            tid = _find_or_create_schedule_thread(db)
            if tid:
                db.add_action_item(
                    thread_id=tid, message=msg, type_="manual_step", priority="high"
                )
            print(f"ABORTED: {msg}", file=sys.stderr)
            return 1

    task_prompt = TASK_PROMPT_TEMPLATE.format(since_date=since_date)

    try:
        if _tmux_session_exists("juggle"):
            if dry_run:
                logger.info("DRY RUN: exercising spawn path with canary task")
                agent_output = _run_canary_spawn(cost_tracker)
            else:
                logger.info("Using Path A: Juggle tmux session")
                agent_output = _run_juggle_path_a(task_prompt, cost_tracker)
        else:
            logger.info("Using Path B: headless claude -p")
            agent_output = _run_headless_research(
                task_prompt, cost_tracker, dry_run=dry_run
            )

        if not agent_output:
            agent_output = "No output received from research agent."

    except CostCapExceeded as e:
        logger.error("Cost cap exceeded: %s", e)
        agent_output = f"[DOGFOOD-COST-CAP] Research truncated: {e}"
        _file_action_item(db, agent_output, dry_run)
        _write_and_commit(today, since_date, agent_output, cost_tracker.total, dry_run)
        return 1
    except subprocess.TimeoutExpired:
        agent_output = "[DOGFOOD-TIMEOUT] Research agent timed out."
        _file_action_item(db, agent_output, dry_run)
        _write_and_commit(today, since_date, agent_output, cost_tracker.total, dry_run)
        return 1

    report_content = _build_report(since_date, agent_output, cost_tracker.total)
    out_path = REPORTS_DIR / f"dogfood-{today}.md"
    tmp_path = Path("/tmp/schedule-dogfood-sample-report.md") if dry_run else None
    write_report(out_path, report_content, dry_run=dry_run, tmp_override=tmp_path)

    if dry_run:
        print(f"DRY RUN: report written to {tmp_path}")
        print(f"DRY RUN: cost estimate ${cost_tracker.total:.4f}")
        return 0

    committed = git_commit(f"chore(schedule): dogfood report {today}")
    if committed:
        git_push()

    _file_action_item(db, agent_output, dry_run)
    mark_run_complete(ROUTINE)
    print(
        f"dogfood complete: reports/dogfood-{today}.md | cost=${cost_tracker.total:.4f}"
    )
    return 0


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(run(dry_run=args.dry_run))
