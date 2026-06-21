#!/usr/bin/env python3
"""Shared plumbing for /schedule:* routines: state persistence, CostTracker
budget guard, gh CLI wrappers, cost-tracked claude_p (via llm_calls), DB/report/
git helpers. Routine-specific logic lives in schedules/{autofix,dogfood,reflect}.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

JUGGLE_DIR = Path.home() / ".juggle"
STATE_FILE = JUGGLE_DIR / "schedule_state.json"
JUGGLE_REPO = Path(__file__).parent.parent.parent  # schedules/ -> src/ -> repo root
REPORTS_DIR = JUGGLE_REPO / "reports"


def _ensure_dirs() -> None:
    JUGGLE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# State — idempotency (last successful run per routine)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    _ensure_dirs()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_run_complete(routine: str) -> None:
    state = load_state()
    state[routine] = {"last_success": datetime.now(timezone.utc).isoformat()}
    save_state(state)


def last_run_ts(routine: str) -> datetime | None:
    state = load_state()
    entry = state.get(routine, {})
    ts = entry.get("last_success")
    if ts:
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Cost cap kill-switch
# ---------------------------------------------------------------------------

class CostCapExceeded(Exception):
    pass


class CostTracker:
    def __init__(self, cap_usd: float, routine: str, dry_run: bool = False):
        self.cap_usd = cap_usd
        self.routine = routine
        self.dry_run = dry_run
        self._total = 0.0

    def add(self, usd: float) -> None:
        self._total += usd
        logger.debug("cost_tracker: %s total=%.4f cap=%.2f", self.routine, self._total, self.cap_usd)
        if not self.dry_run and self._total > self.cap_usd:
            raise CostCapExceeded(
                f"{self.routine}: cost cap ${self.cap_usd:.2f} exceeded "
                f"(accumulated ${self._total:.4f})"
            )

    @property
    def total(self) -> float:
        return self._total

    def estimate_from_tokens(self, input_tokens: int, output_tokens: int,
                              model: str = "claude-sonnet-4-6") -> float:
        # Approximate pricing (per-million tokens): Sonnet input $3, output $15
        if "haiku" in model:
            in_rate, out_rate = 0.80, 4.0
        else:
            in_rate, out_rate = 3.0, 15.0
        cost = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
        return cost


# ---------------------------------------------------------------------------
# gh CLI wrappers
# ---------------------------------------------------------------------------

def gh_run(args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["gh"] + args
    logger.debug("gh_run: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def gh_issue_exists(title: str, days: int = 30) -> bool:
    """Return True if an issue with this exact title exists within the last `days` days."""
    try:
        result = gh_run(["issue", "list", "--state", "all", "--search", title, "--json", "title,createdAt"])
        issues = json.loads(result.stdout or "[]")
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        for iss in issues:
            if iss.get("title", "").strip() == title.strip():
                created = iss.get("createdAt", "")
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    if ts > cutoff:
                        return True
                except Exception:
                    pass
    except Exception as e:
        logger.warning("gh_issue_exists check failed: %s", e)
    return False


def gh_create_issue(title: str, body: str, labels: list[str] | None = None,
                    dry_run: bool = False) -> str | None:
    """Create a GitHub issue. Returns issue URL or None."""
    if dry_run:
        logger.info("DRY RUN: would create issue %r", title)
        return None
    cmd = ["issue", "create", "--title", title, "--body", body]
    if labels:
        for lbl in labels:
            try:
                _ensure_gh_label(lbl)
            except Exception:
                logger.warning("label creation failed for %r, skipping", lbl)
        cmd += ["--label", ",".join(labels)]
    try:
        result = gh_run(cmd)
        url = result.stdout.strip()
        logger.info("created issue: %s", url)
        return url
    except Exception as e:
        logger.error("gh_create_issue failed: %s", e)
        return None


def _ensure_gh_label(label: str) -> None:
    try:
        gh_run(["label", "create", label, "--force"], check=False)
    except Exception:
        pass


def gh_pr_list_head(head_prefix: str) -> list[dict]:
    try:
        result = gh_run(["pr", "list", "--head", head_prefix, "--json", "number,title,state,url,headRefName"])
        return json.loads(result.stdout or "[]")
    except Exception as e:
        logger.warning("gh_pr_list_head failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Claude CLI wrapper (headless -p mode)
# ---------------------------------------------------------------------------

def claude_p(prompt: str, model: str = "claude-sonnet-4-6",
             cost_tracker: CostTracker | None = None,
             timeout: int = 120) -> str:
    """Run `claude -p <prompt>` and return stdout. Updates cost_tracker if provided.

    Thin wrapper over llm_calls.run_claude_p (single source of truth):
    JSON output mode, cost-tracked, returns "" on non-zero exit.
    """
    from llm_calls import run_claude_p

    out = run_claude_p(
        prompt,
        model=model,
        timeout=timeout,
        output_format="json",
        cost_tracker=cost_tracker,
        log=logger,
    )
    return "" if out is None else out


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    src = Path(__file__).parent.parent  # schedules/ -> src/
    sys.path.insert(0, str(src))
    from juggle_db import JuggleDB
    from juggle_db import DB_PATH
    db_path = os.environ.get("_JUGGLE_TEST_DB", str(DB_PATH))
    return JuggleDB(db_path)


def db_query(db, sql: str, params=()) -> list[dict]:
    with db._connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def has_busy_agents(db) -> bool:
    """Return True if any agent currently has status='busy'."""
    try:
        rows = db_query(db, "SELECT id FROM agents WHERE status = 'busy' LIMIT 1")
        return len(rows) > 0
    except Exception as e:
        logger.warning("has_busy_agents check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def days_ago_iso(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# File writing helper
# ---------------------------------------------------------------------------

def write_report(path: Path, content: str, dry_run: bool = False, tmp_override: Path | None = None) -> Path:
    if dry_run and tmp_override:
        tmp_override.parent.mkdir(parents=True, exist_ok=True)
        tmp_override.write_text(content)
        logger.info("DRY RUN: wrote %s", tmp_override)
        return tmp_override
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    logger.info("wrote %s", path)
    return path


def dry_run_sample_path(filename: str) -> Path:
    """Path for a dry-run's throwaway SAMPLE report.

    Defaults to /tmp (so an operator's `--dry-run` drops a sample they can
    eyeball). Overridable via JUGGLE_SCHEDULE_SAMPLE_DIR — read lazily so tests
    can point it at a fresh tmp_path, isolating the sample so its existence
    assertion can't false-green on a stale file from a prior run (M1, 2026-06-21).
    """
    return Path(os.environ.get("JUGGLE_SCHEDULE_SAMPLE_DIR", "/tmp")) / filename


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"] + args
    cwd = cwd or JUGGLE_REPO
    return subprocess.run(cmd, capture_output=True, text=True, check=check, cwd=str(cwd))


def git_commit(message: str, cwd: Path | None = None,
               paths: list[str] | None = None) -> bool:
    cwd = cwd or JUGGLE_REPO
    try:
        if paths:
            git_run(["add", "--"] + paths, cwd=cwd)
        else:
            git_run(["add", "-A"], cwd=cwd)
        result = git_run(["diff", "--cached", "--quiet"], cwd=cwd, check=False)
        if result.returncode == 0:
            logger.info("git_commit: nothing to commit")
            return False
        git_run(["commit", "-m", message], cwd=cwd)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("git_commit failed: %s", e.stderr)
        return False


def git_push(cwd: Path | None = None) -> bool:
    cwd = cwd or JUGGLE_REPO
    try:
        result = git_run(["push", "origin", "main"], cwd=cwd, check=False)
        if result.returncode != 0:
            logger.warning("git push rejected, pulling and retrying: %s", result.stderr)
            git_run(["pull", "--rebase", "origin", "main"], cwd=cwd)
            git_run(["push", "origin", "main"], cwd=cwd)
        return True
    except subprocess.CalledProcessError as e:
        logger.error("git_push failed: %s", e.stderr)
        return False
