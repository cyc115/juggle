#!/usr/bin/env python3
"""
/schedule:reflect — Monday 03:00 local (0 3 * * 1 / UTC: 0 8 * * 1)

Queries Juggle DB + Hindsight + auto-memory to produce a weekly digest, then
commits it and files up to 5 GitHub issues.
"""

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from juggle_schedule_common import (  # noqa: E402
    CostCapExceeded,
    CostTracker,
    JUGGLE_REPO,
    REPORTS_DIR,
    claude_p,
    days_ago_iso,
    db_query,
    get_db,
    gh_create_issue,
    gh_issue_exists,
    gh_pr_list_head,
    git_commit,
    git_push,
    mark_run_complete,
    today_str,
    write_report,
)

COST_CAP = 2.00
ROUTINE = "reflect"
MAX_ISSUES = 5
ISSUE_PRIORITY = ["RF-1", "RF-7", "RF-2", "RF-5", "RF-8"]


# ---------------------------------------------------------------------------
# RF-1: watchdog telemetry
# ---------------------------------------------------------------------------

def rf1_watchdog(db, cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-1: watchdog telemetry")
    since = days_ago_iso(7)
    try:
        events = db_query(
            db,
            "SELECT event_type, COUNT(*) as cnt FROM watchdog_events "
            "WHERE created_at > ? GROUP BY event_type ORDER BY cnt DESC",
            (since,)
        )
    except Exception as e:
        logger.warning("RF-1 query failed: %s", e)
        sections["RF-1"] = "## Watchdog Health\n\n*DB query failed — unable to retrieve telemetry.*\n"
        return

    if not events:
        sections["RF-1"] = "## Watchdog Health\n\n*No watchdog events this week — quiet week, positive signal.*\n"
        return

    events_summary = "\n".join(f"- {row['event_type']}: {row['cnt']}" for row in events)
    prompt = (
        f"Analyze these watchdog event counts from the last 7 days of Juggle operation.\n"
        f"Identify: top 3 failure modes, re-dispatch success rate (if data available), "
        f"threshold tuning suggestions.\n"
        f"Keep response under 200 words.\n\nEvents:\n{events_summary}"
    )
    try:
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-1"] = f"## Watchdog Health\n\n{analysis}\n\n**Raw event counts:**\n{events_summary}\n"
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("RF-1 LLM failed: %s", e)
        sections["RF-1"] = f"## Watchdog Health\n\n**Raw event counts:**\n{events_summary}\n"


# ---------------------------------------------------------------------------
# RF-2: action item ack patterns
# ---------------------------------------------------------------------------

def rf2_action_items(db, cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-2: action item ack patterns")
    since = days_ago_iso(30)
    try:
        items = db_query(
            db,
            "SELECT type, priority, created_at, dismissed_at FROM action_items WHERE created_at > ?",
            (since,)
        )
    except Exception as e:
        logger.warning("RF-2 query failed: %s", e)
        sections["RF-2"] = "## Action Item Fatigue\n\n*DB query failed.*\n"
        return

    if not items:
        sections["RF-2"] = "## Action Item Fatigue\n\n*No action items in the last 30 days.*\n"
        return

    total = len(items)
    dismissed = sum(1 for i in items if i.get("dismissed_at"))
    by_type: dict[str, int] = {}
    for i in items:
        t = i.get("type", "unknown")
        by_type[t] = by_type.get(t, 0) + 1

    summary = (
        f"Total: {total} | Dismissed: {dismissed} | Pending: {total - dismissed}\n"
        + "\n".join(f"- {k}: {v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1]))
    )
    prompt = (
        f"Analyze these action item patterns from the last 30 days of Juggle usage.\n"
        f"Identify: types that pile up, items dismissed quickly (noise), keyword tuning suggestions.\n"
        f"Under 150 words.\n\n{summary}"
    )
    try:
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-2"] = f"## Action Item Fatigue\n\n{analysis}\n\n**Stats:**\n{summary}\n"
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("RF-2 LLM failed: %s", e)
        sections["RF-2"] = f"## Action Item Fatigue\n\n**Stats:**\n{summary}\n"


# ---------------------------------------------------------------------------
# RF-3: agent output quality
# ---------------------------------------------------------------------------

def rf3_completion_quality(db, cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-3: agent output quality")
    since = days_ago_iso(7)
    try:
        completions = db_query(
            db,
            "SELECT role, duration_secs, completed_at FROM agent_completions "
            "WHERE completed_at > ? ORDER BY completed_at DESC LIMIT 20",
            (since,)
        )
    except Exception as e:
        logger.warning("RF-3 query failed: %s", e)
        sections["RF-3"] = "## Agent Output Quality\n\n*DB query failed.*\n"
        return

    if not completions:
        sections["RF-3"] = "## Agent Output Quality\n\n*No completions this week.*\n"
        return

    durations = [c.get("duration_secs", 0) for c in completions]
    avg_dur = sum(durations) / len(durations) if durations else 0
    role_counts: dict[str, int] = {}
    for c in completions:
        r = c.get("role", "unknown")
        role_counts[r] = role_counts.get(r, 0) + 1

    summary = (
        f"Completions: {len(completions)} | Avg duration: {avg_dur:.0f}s\n"
        + "\n".join(f"- {k}: {v}" for k, v in sorted(role_counts.items(), key=lambda x: -x[1]))
    )
    prompt = (
        f"Rate these Juggle agent completion stats on a 1-5 completeness scale.\n"
        f"Identify outliers (very long durations, unexpected role distributions).\n"
        f"Under 150 words.\n\n{summary}"
    )
    try:
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-3"] = f"## Agent Output Quality\n\n{analysis}\n\n**Stats:**\n{summary}\n"
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("RF-3 LLM failed: %s", e)
        sections["RF-3"] = f"## Agent Output Quality\n\n**Stats:**\n{summary}\n"


# ---------------------------------------------------------------------------
# RF-4: context bloat
# ---------------------------------------------------------------------------

def rf4_context_bloat(db, sections: dict) -> None:
    logger.info("RF-4: context bloat candidates")
    since = days_ago_iso(7)
    try:
        rows = db_query(
            db,
            "SELECT thread_id, COUNT(*) as msg_count FROM messages "
            "WHERE created_at > ? GROUP BY thread_id ORDER BY msg_count DESC LIMIT 5",
            (since,)
        )
    except Exception as e:
        logger.info("RF-4 query skipped: %s", e)
        sections["RF-4"] = "## Context Bloat Candidates\n\n*Messages table not available.*\n"
        return

    if not rows:
        sections["RF-4"] = "## Context Bloat Candidates\n\n*No messages this week.*\n"
        return

    lines = [f"- Thread {r['thread_id'][:8]}: {r['msg_count']} messages" for r in rows]
    sections["RF-4"] = "## Context Bloat Candidates\n\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# RF-5: Hindsight memory lint
# ---------------------------------------------------------------------------

def rf5_hindsight_lint(cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-5: Hindsight memory lint")
    try:
        from juggle_hindsight import HindsightClient
        client = HindsightClient.from_config()
        if client is None:
            sections["RF-5"] = "## Memory Health\n\n*Hindsight unavailable this week.*\n"
            return

        memories = client.search("", limit=50)
        if not memories:
            sections["RF-5"] = "## Memory Health\n\n*No Hindsight memories found.*\n"
            return

        since = days_ago_iso(60)
        old_memories = [m for m in memories if m.get("created_at", "") < since]
        if not old_memories:
            sections["RF-5"] = "## Memory Health\n\n*No memories older than 60 days.*\n"
            return

        memory_text = "\n".join(
            f"- [{m.get('id','')}] {m.get('content','')[:100]}"
            for m in old_memories[:20]
        )
        prompt = (
            f"Review these Hindsight memories (>60 days old) for: contradictions, "
            f"stale code references, near-duplicates.\n"
            f"Suggest which to archive or merge. Under 200 words.\n\n{memory_text}"
        )
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-5"] = f"## Memory Health\n\n{analysis}\n"
    except CostCapExceeded:
        raise
    except ImportError:
        sections["RF-5"] = "## Memory Health\n\n*Hindsight module not available.*\n"
    except Exception as e:
        logger.warning("RF-5 failed: %s", e)
        sections["RF-5"] = f"## Memory Health\n\n*Hindsight unavailable this week: {e}*\n"


# ---------------------------------------------------------------------------
# RF-6: auto-memory scan (suggestions only)
# ---------------------------------------------------------------------------

def rf6_auto_memory(cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-6: auto-memory scan")
    memory_dirs = list(Path.home().glob(".claude/projects/*/memory/"))
    if not memory_dirs:
        sections["RF-6"] = "## Auto-Memory Contradictions\n\n*No auto-memory directories found.*\n"
        return

    all_memories = []
    for d in memory_dirs[:2]:
        for f in d.glob("*.md"):
            try:
                content = f.read_text(errors="ignore")[:200]
                all_memories.append(f"[{f.stem}] {content}")
            except Exception:
                pass

    if not all_memories:
        sections["RF-6"] = "## Auto-Memory Contradictions\n\n*No memory files found.*\n"
        return

    memory_text = "\n".join(all_memories[:30])
    prompt = (
        f"Review these auto-memory files for: contradictions, stale references, near-duplicates.\n"
        f"Output SUGGESTIONS ONLY — do not edit any files.\nUnder 200 words.\n\n{memory_text}"
    )
    try:
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-6"] = (
            f"## Auto-Memory Contradictions\n\n{analysis}\n\n"
            f"*Note: suggestions only — no files modified.*\n"
        )
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("RF-6 LLM failed: %s", e)
        sections["RF-6"] = "## Auto-Memory Contradictions\n\n*Analysis failed.*\n"


# ---------------------------------------------------------------------------
# RF-7: skill description drift
# ---------------------------------------------------------------------------

def rf7_skill_drift(db, cost_tracker: CostTracker, sections: dict) -> None:
    logger.info("RF-7: skill description drift")
    skills_dir = JUGGLE_REPO / "skills"
    if not skills_dir.exists():
        sections["RF-7"] = "## Skill Drift\n\n*Skills directory not found.*\n"
        return

    skill_descriptions = {}
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            for line in skill_md.read_text(errors="ignore").splitlines():
                if line.startswith("description:"):
                    skill_descriptions[skill_dir.name] = line.replace("description:", "").strip()
                    break

    if not skill_descriptions:
        sections["RF-7"] = "## Skill Drift\n\n*No skill descriptions found.*\n"
        return

    try:
        rows = db_query(db, "SELECT task FROM agents WHERE task IS NOT NULL ORDER BY updated_at DESC LIMIT 20")
        task_prompts = [r.get("task", "") for r in rows if r.get("task")]
    except Exception:
        task_prompts = []

    if not task_prompts:
        sections["RF-7"] = "## Skill Drift\n\n*No agent task prompts in DB to compare against.*\n"
        return

    skill_list = "\n".join(f"- {k}: {v}" for k, v in list(skill_descriptions.items())[:10])
    tasks_sample = "\n".join(task_prompts[:5])[:1000]

    prompt = (
        f"Compare these skill descriptions against recent agent task prompts.\n"
        f"Identify skills whose description doesn't match how they're actually being used.\n"
        f"Under 150 words.\n\nSkill descriptions:\n{skill_list}\n\nRecent tasks:\n{tasks_sample}"
    )
    try:
        analysis = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
        sections["RF-7"] = f"## Skill Drift\n\n{analysis}\n"
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("RF-7 LLM failed: %s", e)
        sections["RF-7"] = "## Skill Drift\n\n*Analysis failed.*\n"


# ---------------------------------------------------------------------------
# RF-8: dogfood cross-link
# ---------------------------------------------------------------------------

def rf8_dogfood_pulse(sections: dict) -> None:
    logger.info("RF-8: dogfood pulse")
    reports = sorted(REPORTS_DIR.glob("dogfood-*.md"), key=lambda p: p.name, reverse=True)
    if not reports:
        sections["RF-8"] = "## Dogfood Pulse\n\n*No dogfood reports found this week.*\n"
        return

    latest = reports[0]
    content = latest.read_text(errors="ignore")
    suggestion_excerpt = ""
    in_suggestions = False
    for line in content.splitlines():
        if "## Suggested Improvements" in line:
            in_suggestions = True
            continue
        if in_suggestions and line.strip():
            suggestion_excerpt += line + "\n"
        if len(suggestion_excerpt) > 400:
            break

    sections["RF-8"] = (
        f"## Dogfood Pulse\n\n"
        f"Most recent dogfood report: `reports/{latest.name}`\n\n"
        f"**Key suggestions:**\n{suggestion_excerpt or '*(none found)*'}\n"
    )


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------

def _build_digest(today: str, sections: dict, autofix_pr_ref: str) -> str:
    since = days_ago_iso(7)[:10]
    ordered = ["RF-1", "RF-2", "RF-3", "RF-4", "RF-5", "RF-6", "RF-7", "RF-8"]
    header = (
        f"# Juggle Weekly Digest — {today}\n\n"
        f"> Generated by `/schedule:reflect` via Claude Code Routines.\n"
        f"> This digest covers {since} through {today}.\n"
        f"> Autofix PR this week: {autofix_pr_ref}\n\n"
    )
    body = "\n\n".join(sections.get(k, f"## {k}\n\n*Not run.*\n") for k in ordered)
    return header + body + "\n"


def _find_autofix_pr_ref() -> str:
    prs = gh_pr_list_head("cyc_schedule-autofix-")
    if not prs:
        return "not run this week"
    pr = prs[0]
    return f"#{pr.get('number', '?')} ({pr.get('state', '?')})"


# ---------------------------------------------------------------------------
# Issue filing
# ---------------------------------------------------------------------------

def _file_reflect_issues(sections: dict, today: str, report_path: Path, dry_run: bool) -> list[str]:
    filed = []
    for section_id in ISSUE_PRIORITY:
        if len(filed) >= MAX_ISSUES:
            break
        content = sections.get(section_id, "")
        if not content or "*Not run.*" in content or "No " in content[:50]:
            continue
        title = f"reflect: {section_id} finding — {today}"[:72]
        if gh_issue_exists(title, days=30):
            logger.info("reflect issue already exists (skip): %s", title)
            continue
        body = f"{content[:500]}\n\nFull digest: `reports/reflect-{today}.md`"
        url = gh_create_issue(title, body, labels=["routine-reflect"], dry_run=dry_run)
        if url:
            filed.append(url)
    return filed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> int:
    today = today_str()
    cost_tracker = CostTracker(cap_usd=COST_CAP, routine=ROUTINE, dry_run=dry_run)
    sections: dict = {}
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    db = get_db()
    autofix_ref = _find_autofix_pr_ref()

    for fn, key in [
        (lambda: rf1_watchdog(db, cost_tracker, sections), "RF-1"),
        (lambda: rf2_action_items(db, cost_tracker, sections), "RF-2"),
        (lambda: rf3_completion_quality(db, cost_tracker, sections), "RF-3"),
        (lambda: rf4_context_bloat(db, sections), "RF-4"),
        (lambda: rf5_hindsight_lint(cost_tracker, sections), "RF-5"),
        (lambda: rf6_auto_memory(cost_tracker, sections), "RF-6"),
        (lambda: rf7_skill_drift(db, cost_tracker, sections), "RF-7"),
        (lambda: rf8_dogfood_pulse(sections), "RF-8"),
    ]:
        try:
            fn()
        except CostCapExceeded as e:
            logger.error("Cost cap hit at %s: %s", key, e)
            sections.setdefault(key, f"## {key}\n\n*[COST CAP]*\n")
            break

    digest = _build_digest(today, sections, autofix_ref)
    out_path = REPORTS_DIR / f"reflect-{today}.md"
    tmp_path = Path("/tmp/schedule-reflect-sample-digest.md") if dry_run else None
    write_report(out_path, digest, dry_run=dry_run, tmp_override=tmp_path)

    if dry_run:
        print(f"DRY RUN: digest written to {tmp_path}")
        print(f"DRY RUN: cost estimate ${cost_tracker.total:.4f}")
        return 0

    committed = git_commit(f"chore(schedule): reflect digest {today}")
    if committed:
        git_push()

    issued = _file_reflect_issues(sections, today, out_path, dry_run=False)
    logger.info("reflect: filed %d issues", len(issued))

    mark_run_complete(ROUTINE)
    print(f"reflect complete: reports/reflect-{today}.md | {len(issued)} issues | cost=${cost_tracker.total:.4f}")
    return 0


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(run(dry_run=args.dry_run))
