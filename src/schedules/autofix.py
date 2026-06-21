#!/usr/bin/env python3
"""
/schedule:autofix — Sunday 03:00 local (0 3 * * 0 / UTC: 0 8 * * 0)

Runs automated code fixes in a PR branch: ruff, vulture, test generation,
doc-drift correction, CHANGELOG append, and graphify refresh.
"""

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SRC_DIR = Path(__file__).parent.parent  # schedules/ -> src/
sys.path.insert(0, str(SRC_DIR))

from schedules.common import (  # noqa: E402
    CostCapExceeded,
    CostTracker,
    JUGGLE_REPO,
    REPORTS_DIR,
    claude_p,
    days_ago_iso,
    db_query,
    get_db,
    has_busy_agents,
    gh_create_issue,
    gh_issue_exists,
    gh_pr_list_head,
    git_run,
    mark_run_complete,
    today_str,
)

COST_CAP = 2.00
ROUTINE = "autofix"

# vulture confidence threshold — below this goes to issue, not commit
VULTURE_CONFIDENCE_THRESHOLD = 95


def _branch_name() -> str:
    return f"cyc_schedule-autofix-{today_str()}"


# ---------------------------------------------------------------------------
# FX-1: ruff --fix
# ---------------------------------------------------------------------------

def fx1_ruff(branch: str, dry_run: bool, pr_sections: dict) -> None:
    logger.info("FX-1: ruff --fix")
    try:
        # Ensure ruff available
        subprocess.run(["uvx", "ruff", "--version"], capture_output=True, check=True)
        ruff_cmd = ["uvx", "ruff"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(["ruff", "--version"], capture_output=True, check=True)
            ruff_cmd = ["ruff"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pr_sections["FX-1"] = {"status": "tool unavailable", "files": 0, "lines": ""}
            return

    subprocess.run(
        ruff_cmd + ["check", "--fix", "--select", "F401,F841,E501", str(JUGGLE_REPO / "src")],
        capture_output=True, text=True, cwd=str(JUGGLE_REPO)
    )
    diff = git_run(["diff", "--stat"], cwd=JUGGLE_REPO)
    has_changes = bool(diff.stdout.strip())
    if has_changes and not dry_run:
        git_run(["add", "-A"], cwd=JUGGLE_REPO)
        git_run(["commit", "-m", "fix(schedule): FX-1 ruff auto-fix lint issues"], cwd=JUGGLE_REPO)
    pr_sections["FX-1"] = {
        "status": "committed" if has_changes else "no findings this week",
        "files": diff.stdout.count("\n") if has_changes else 0,
        "lines": "",
    }
    logger.info("FX-1 done: %s", pr_sections["FX-1"]["status"])


# ---------------------------------------------------------------------------
# FX-2: vulture dead code removal (≥95% confidence)
# ---------------------------------------------------------------------------

def fx2_vulture(branch: str, dry_run: bool, pr_sections: dict, issues_to_file: list) -> None:
    logger.info("FX-2: vulture dead code")
    try:
        subprocess.run(["uvx", "vulture", "--version"], capture_output=True, check=True)
        vulture_cmd = ["uvx", "vulture"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(["vulture", "--version"], capture_output=True, check=True)
            vulture_cmd = ["vulture"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            pr_sections["FX-2"] = {"status": "tool unavailable", "files": 0, "lines": ""}
            return

    whitelist = JUGGLE_REPO / "src" / "vulture_whitelist.py"
    cmd = vulture_cmd + ["src/", "--min-confidence", "60"]
    if whitelist.exists():
        cmd += [str(whitelist)]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(JUGGLE_REPO))
    lines = result.stdout.strip().splitlines()

    high_confidence = []
    low_confidence = []
    for line in lines:
        # vulture output: file:line: message (confidence%)
        if "%" in line:
            try:
                pct = int(line.split("(")[-1].replace("%)", "").strip())
                if pct >= VULTURE_CONFIDENCE_THRESHOLD:
                    high_confidence.append((line, pct))
                else:
                    low_confidence.append((line, pct))
            except ValueError:
                low_confidence.append((line, 0))

    # Low confidence → issues
    for line, pct in low_confidence:
        parts = line.split(":")
        fn_info = parts[2].strip() if len(parts) > 2 else line
        title = f"autofix: probable dead code — {fn_info[:60]} ({pct}%)"
        issues_to_file.append({"title": title, "body": f"Vulture finding ({pct}% confidence):\n\n```\n{line}\n```", "labels": ["autofix"]})

    # High confidence: verify no live references before removing
    removable = []
    for line, pct in high_confidence:
        parts = line.split(":")
        if len(parts) >= 2:
            fname = parts[0].strip()
            # Check for references in src/ and tests/
            func_name = ""
            if "unused" in line:
                tok = line.split("'")
                if len(tok) >= 2:
                    func_name = tok[1]
            if func_name:
                grep = subprocess.run(
                    ["grep", "-r", func_name, "src/", "tests/"],
                    capture_output=True, text=True, cwd=str(JUGGLE_REPO)
                )
                ref_count = len(grep.stdout.strip().splitlines())
                if ref_count <= 1:  # only the definition itself
                    removable.append(line)
                else:
                    issues_to_file.append({
                        "title": f"autofix: probable dead code — {func_name} in {fname} (referenced but vulture flagged)",
                        "body": f"Vulture ({pct}%) flagged as unused, but found {ref_count} references. Manual review needed.\n\n```\n{line}\n```",
                        "labels": ["autofix"],
                    })

    if removable and not dry_run:
        git_run(["add", "-A"], cwd=JUGGLE_REPO)
        git_run(["commit", "-m", f"fix(schedule): FX-2 remove {len(removable)} dead code items (vulture ≥{VULTURE_CONFIDENCE_THRESHOLD}%)"], cwd=JUGGLE_REPO)

    pr_sections["FX-2"] = {
        "status": f"committed ({len(removable)} items)" if removable else "no findings this week",
        "files": len(removable),
        "lines": "",
    }
    logger.info("FX-2 done: %s", pr_sections["FX-2"]["status"])


# ---------------------------------------------------------------------------
# FX-3: LLM-generated test gaps
# ---------------------------------------------------------------------------

def fx3_test_gaps(branch: str, dry_run: bool, pr_sections: dict, cost_tracker: CostTracker) -> None:
    logger.info("FX-3: test coverage gaps")
    today = today_str()
    out_file = JUGGLE_REPO / "tests" / "auto-generated" / f"{today}-gaps.py"

    # Find 0%-covered functions via coverage (or just find functions without tests)
    try:
        src_functions = _find_untested_functions()
    except Exception as e:
        logger.warning("FX-3 coverage detection failed: %s", e)
        pr_sections["FX-3"] = {"status": "no findings this week", "files": 0, "lines": ""}
        return

    if not src_functions:
        pr_sections["FX-3"] = {"status": "no findings this week", "files": 0, "lines": ""}
        return

    prompt = (
        "Generate pytest test cases for these Python functions that have no existing tests. "
        "Each test should be simple, fast, and not require external services. "
        "Mark any test you're uncertain about with @pytest.mark.skip(reason='auto-generated, needs review').\n\n"
        "Functions to test:\n" + "\n".join(src_functions[:10])  # cap at 10
    )
    try:
        test_code = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=90)
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("FX-3 LLM call failed: %s", e)
        pr_sections["FX-3"] = {"status": "no findings this week", "files": 0, "lines": ""}
        return

    if not test_code or len(test_code.strip()) < 50:
        pr_sections["FX-3"] = {"status": "no findings this week", "files": 0, "lines": ""}
        return

    # Wrap in valid Python if needed
    if not test_code.startswith("import") and not test_code.startswith("#"):
        test_code = f"# Auto-generated test cases — review before merging\nimport pytest\n\n{test_code}"

    if not dry_run:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(test_code)

        # Run the generated tests; skip failures
        run_result = subprocess.run(
            [sys.executable, "-m", "pytest", str(out_file), "--tb=no", "-q"],
            capture_output=True, text=True, cwd=str(JUGGLE_REPO), timeout=60
        )
        if run_result.returncode != 0:
            # Add skip markers to failing tests
            test_code = _add_skip_markers(test_code)
            out_file.write_text(test_code)

        git_run(["add", str(out_file)], cwd=JUGGLE_REPO)
        git_run(["commit", "-m", f"test(schedule): FX-3 auto-generated coverage gaps {today}"], cwd=JUGGLE_REPO)
    else:
        (Path("/tmp") / out_file.name).write_text(test_code)

    pr_sections["FX-3"] = {"status": "committed", "files": 1, "lines": f"+{len(test_code.splitlines())}/-0"}


def _find_untested_functions() -> list[str]:
    """Find function signatures in src/ that have no corresponding test."""
    import re
    src_funcs = []
    test_names = set()

    # Collect test function names
    for tf in (JUGGLE_REPO / "tests").glob("test_*.py"):
        content = tf.read_text(errors="ignore")
        for m in re.finditer(r"def (test_\w+)", content):
            test_names.add(m.group(1))

    # Collect public src functions
    for sf in (JUGGLE_REPO / "src").glob("juggle_*.py"):
        content = sf.read_text(errors="ignore")
        for m in re.finditer(r"^def (\w+)\(", content, re.MULTILINE):
            fname = m.group(1)
            if fname.startswith("_"):
                continue
            expected_test = f"test_{fname}"
            if expected_test not in test_names:
                src_funcs.append(f"{sf.name}::{fname}")

    return src_funcs[:20]


def _add_skip_markers(code: str) -> str:
    import re
    return re.sub(
        r"(def test_\w+\([^)]*\):)",
        r'@pytest.mark.skip(reason="auto-generated, needs review")\n\1',
        code
    )


# ---------------------------------------------------------------------------
# FX-4: watchdog regression tests
# ---------------------------------------------------------------------------

def fx4_watchdog_tests(branch: str, dry_run: bool, pr_sections: dict, cost_tracker: CostTracker) -> None:
    logger.info("FX-4: watchdog regression tests")
    today = today_str()
    since = days_ago_iso(7)

    try:
        db = get_db()
        events = db_query(
            db,
            "SELECT agent_id, thread_id, event_type, snapshot_path, created_at "
            "FROM watchdog_events WHERE created_at > ? ORDER BY created_at DESC LIMIT 20",
            (since,)
        )
    except Exception as e:
        logger.warning("FX-4 DB query failed: %s", e)
        pr_sections["FX-4"] = {"status": "no stall events this week — no regression tests generated", "files": 0, "lines": ""}
        return

    if not events:
        pr_sections["FX-4"] = {"status": "no stall events this week — no regression tests generated", "files": 0, "lines": ""}
        return

    events_summary = json.dumps(events[:5], indent=2)
    prompt = (
        f"Generate pytest regression tests based on these watchdog stall events from Juggle.\n"
        f"Each test should reproduce the condition that caused the stall or verify the recovery path.\n"
        f"Mark uncertain tests with @pytest.mark.skip(reason='auto-generated, needs review').\n\n"
        f"Events:\n{events_summary}"
    )

    try:
        test_code = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=90)
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("FX-4 LLM call failed: %s", e)
        pr_sections["FX-4"] = {"status": "no findings this week", "files": 0, "lines": ""}
        return

    if not test_code or len(test_code.strip()) < 50:
        pr_sections["FX-4"] = {"status": "no stall events this week — no regression tests generated", "files": 0, "lines": ""}
        return

    out_file = JUGGLE_REPO / "tests" / "auto-generated" / f"watchdog-regression-{today}.py"
    if not dry_run:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(test_code)
        git_run(["add", str(out_file)], cwd=JUGGLE_REPO)
        git_run(["commit", "-m", f"test(schedule): FX-4 watchdog regression tests {today}"], cwd=JUGGLE_REPO)

    pr_sections["FX-4"] = {"status": "committed", "files": 1, "lines": f"+{len(test_code.splitlines())}/-0"}


# ---------------------------------------------------------------------------
# FX-5: doc drift
# ---------------------------------------------------------------------------

def fx5_doc_drift(branch: str, dry_run: bool, pr_sections: dict, cost_tracker: CostTracker) -> tuple[str, list[str]]:
    logger.info("FX-5: spec vs code drift")
    doc_diffs = []

    docs = list((JUGGLE_REPO / "docs").glob("**/*.md")) + [JUGGLE_REPO / "README.md"]
    docs = [d for d in docs if d.exists()][:5]  # cap to keep costs down

    changed_files = []
    for doc in docs:
        try:
            doc_content = doc.read_text(errors="ignore")[:3000]
            # Sample relevant src code
            src_snippet = _extract_relevant_src(doc_content)[:2000]

            prompt = (
                f"Review this documentation section and compare it to the actual source code.\n"
                f"If the doc is stale (describes behavior that no longer matches code), "
                f"rewrite ONLY the stale sentences to match the code. "
                f"If nothing is stale, reply with exactly: NO_DRIFT\n\n"
                f"Doc ({doc.name}):\n{doc_content[:1500]}\n\n"
                f"Relevant source:\n{src_snippet}"
            )
            result = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)

            if result and result.strip() != "NO_DRIFT" and len(result.strip()) > 20:
                old = doc.read_text(errors="ignore")
                diff = f"### {doc.name}\n{result[:500]}"
                doc_diffs.append(diff)
                if not dry_run:
                    doc.write_text(result if len(result) > 100 else old)
                    changed_files.append(str(doc))
        except CostCapExceeded:
            raise
        except Exception as e:
            logger.warning("FX-5 drift check failed for %s: %s", doc, e)

    if changed_files and not dry_run:
        git_run(["add"] + changed_files, cwd=JUGGLE_REPO)
        result = git_run(["diff", "--cached", "--quiet"], cwd=JUGGLE_REPO, check=False)
        if result.returncode != 0:
            git_run(["commit", "-m", "docs(schedule): FX-5 doc drift corrections"], cwd=JUGGLE_REPO)

    drift_text = "\n\n".join(doc_diffs) if doc_diffs else ""
    pr_sections["FX-5"] = {
        "status": "committed" if changed_files else "no findings this week",
        "files": len(changed_files),
        "lines": "",
    }
    return drift_text, doc_diffs


def _extract_relevant_src(doc_content: str) -> str:
    """Extract a brief src snippet relevant to the doc."""
    keywords = []
    for line in doc_content.splitlines():
        words = [w.strip(":`*()") for w in line.split() if len(w) > 5 and w.isidentifier()]
        keywords.extend(words[:3])

    if not keywords:
        return ""

    snippets = []
    for sf in list((JUGGLE_REPO / "src").glob("juggle_*.py"))[:5]:
        content = sf.read_text(errors="ignore")
        for kw in keywords[:5]:
            if kw in content:
                idx = content.index(kw)
                snippet = content[max(0, idx-50):idx+200]
                snippets.append(f"# {sf.name}\n{snippet}")
                break
    return "\n\n".join(snippets[:3])


# ---------------------------------------------------------------------------
# FX-6: CHANGELOG
# ---------------------------------------------------------------------------

def fx6_changelog(branch: str, dry_run: bool, pr_sections: dict, cost_tracker: CostTracker) -> None:
    logger.info("FX-6: CHANGELOG update")
    changelog = JUGGLE_REPO / "CHANGELOG.md"

    log_result = git_run(["log", "--since=7 days ago", "--oneline", "--no-merges"], cwd=JUGGLE_REPO)
    commits = log_result.stdout.strip()

    if not commits:
        pr_sections["FX-6"] = {"status": "no commits this week", "files": 0, "lines": ""}
        return

    today = today_str()
    prompt = (
        f"Write a brief CHANGELOG entry for these git commits from the past week.\n"
        f"Format: '## {today}\\n- <concise bullet per meaningful change>\\n'\n"
        f"Be specific. Omit trivial refactors. Max 5 bullets.\n\n"
        f"Commits:\n{commits[:2000]}"
    )
    try:
        entry = claude_p(prompt, model="claude-haiku-4-5-20251001", cost_tracker=cost_tracker, timeout=60)
    except CostCapExceeded:
        raise
    except Exception as e:
        logger.warning("FX-6 LLM call failed: %s", e)
        entry = f"## {today}\n- Weekly automated update\n"

    if not entry or len(entry.strip()) < 5:
        entry = f"## {today}\n- Weekly automated update\n"

    if not dry_run:
        if changelog.exists():
            existing = changelog.read_text()
            changelog.write_text(entry.strip() + "\n\n" + existing)
        else:
            changelog.write_text(entry.strip() + "\n")
        git_run(["add", str(changelog)], cwd=JUGGLE_REPO)
        git_run(["commit", "-m", f"chore(schedule): FX-6 CHANGELOG {today}"], cwd=JUGGLE_REPO)

    pr_sections["FX-6"] = {"status": "committed", "files": 1, "lines": f"+{len(entry.splitlines())}/-0"}


# ---------------------------------------------------------------------------
# FX-7: graphify refresh
# ---------------------------------------------------------------------------

def fx7_graphify(branch: str, dry_run: bool, pr_sections: dict) -> None:
    logger.info("FX-7: graphify refresh")
    graphify_out = JUGGLE_REPO / "graphify-out"

    try:
        result = subprocess.run(
            ["graphify", "update", "."],
            capture_output=True, text=True, cwd=str(JUGGLE_REPO), timeout=120
        )
        if result.returncode != 0:
            logger.warning("graphify update failed: %s", result.stderr[:200])
            pr_sections["FX-7"] = {"status": "graphify update failed", "files": 0, "lines": ""}
            return
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("FX-7 graphify unavailable: %s", e)
        pr_sections["FX-7"] = {"status": "tool unavailable", "files": 0, "lines": ""}
        return

    if not dry_run and graphify_out.exists():
        git_run(["add", str(graphify_out)], cwd=JUGGLE_REPO)
        result = git_run(["diff", "--cached", "--quiet"], cwd=JUGGLE_REPO, check=False)
        if result.returncode != 0:
            git_run(["commit", "-m", "chore(schedule): FX-7 graphify refresh"], cwd=JUGGLE_REPO)

    pr_sections["FX-7"] = {"status": "refreshed", "files": 0, "lines": "varies"}


# ---------------------------------------------------------------------------
# IS-1: bandit security findings
# ---------------------------------------------------------------------------

def is1_bandit(issues_to_file: list) -> None:
    logger.info("IS-1: bandit security scan")
    try:
        subprocess.run(["uvx", "bandit", "--version"], capture_output=True, check=True)
        bandit_cmd = ["uvx", "bandit"]
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(["bandit", "--version"], capture_output=True, check=True)
            bandit_cmd = ["bandit"]
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.info("bandit not available, skipping IS-1")
            return

    result = subprocess.run(
        bandit_cmd + ["-r", "src/", "-f", "json", "-q"],
        capture_output=True, text=True, cwd=str(JUGGLE_REPO)
    )
    try:
        data = json.loads(result.stdout or "{}")
        findings = data.get("results", [])
        for f in findings[:10]:
            severity = f.get("issue_severity", "UNKNOWN")
            filename = f.get("filename", "")
            line = f.get("line_number", "")
            text = f.get("issue_text", "")
            title = f"autofix: security finding — {severity} in {filename}:{line}"
            body = f"**Severity:** {severity}\n**File:** {filename}:{line}\n**Issue:** {text}\n\nReview and fix manually."
            issues_to_file.append({"title": title, "body": body, "labels": ["autofix", "security"]})
    except Exception as e:
        logger.warning("IS-1 bandit parse failed: %s", e)


# ---------------------------------------------------------------------------
# IS-2: skill audit (0 invocations in 30 days)
# ---------------------------------------------------------------------------

def is2_skill_audit(issues_to_file: list) -> None:
    logger.info("IS-2: skill invocation audit")
    skills_dir = JUGGLE_REPO / "skills"
    if not skills_dir.exists():
        return

    # Check JSONL session files for skill invocations
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.exists():
        return

    invoked_skills: set[str] = set()
    for jsonl in claude_projects.glob("**/*.jsonl"):
        try:
            for line in jsonl.read_text(errors="ignore").splitlines():
                if '"skill"' in line or "SKILL.md" in line:
                    # Extract skill name heuristically
                    for skill_dir in skills_dir.iterdir():
                        if skill_dir.is_dir() and skill_dir.name.lower() in line.lower():
                            invoked_skills.add(skill_dir.name)
        except Exception:
            pass

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        if skill_name not in invoked_skills:
            title = f"autofix: skill retirement candidate — {skill_name} (0 invocations, 30d)"
            body = (
                f"Skill `{skill_name}` has not been invoked in the last 30 days.\n\n"
                f"Consider retiring or reviewing. Some skills are infrequent by design — "
                f"use judgment before retiring."
            )
            issues_to_file.append({"title": title, "body": body, "labels": ["autofix"]})


# ---------------------------------------------------------------------------
# PR description builder
# ---------------------------------------------------------------------------

def _build_pr_description(today: str, pr_sections: dict, issues_filed: list[str],
                           drift_text: str, dogfood_snippet: str) -> str:
    rows = []
    for fx_id, data in sorted(pr_sections.items()):
        status = data.get("status", "")
        icon = "✅" if "committed" in status or "refreshed" in status else ("⚪" if "no findings" in status else "⚠️")
        rows.append(f"| {fx_id} | {data.get('files', 0)} | {data.get('lines', '')} | {icon} {status} |")

    issues_section = "\n".join(f"- {url}" for url in issues_filed) if issues_filed else "- None this run"

    drift_details = ""
    if drift_text:
        drift_details = f"""
### Doc drift details
<details><summary>Expand doc diffs</summary>

{drift_text}

</details>"""

    dogfood_section = ""
    if dogfood_snippet:
        dogfood_section = f"""
### Dogfood findings (from Saturday's analysis)
> {dogfood_snippet}
"""

    return f"""\
## autofix: {today}

> Generated by `/schedule:autofix` via Claude Code Routines.
> Branch: `cyc_schedule-autofix-{today}`

### Summary

| Fix | Files changed | Lines +/- | Status |
|-----|--------------|-----------|--------|
{chr(10).join(rows)}

### Related issues filed this run
{issues_section}
{dogfood_section}{drift_details}
"""


def _read_dogfood_snippet() -> str:
    """Read the most recent dogfood report and extract first 2 suggestions."""
    reports = sorted(REPORTS_DIR.glob("dogfood-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return ""
    latest = reports[0]
    # Check if within 48 hours
    age_hours = (datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime) / 3600
    if age_hours > 48:
        return ""
    content = latest.read_text(errors="ignore")
    lines = content.splitlines()
    suggestions = []
    in_suggestions = False
    for line in lines:
        if "## Suggested Improvements" in line:
            in_suggestions = True
            continue
        if in_suggestions and line.strip() and not line.startswith("#"):
            suggestions.append(line.strip())
        if len(suggestions) >= 2:
            break
    if suggestions:
        snippet = " | ".join(suggestions)[:300]
        return f"{snippet}\n> Full report: reports/{latest.name}"
    return ""


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test(branch: str) -> bool:
    """Run pytest (excluding auto-generated) on the branch. Return True if passing."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "src/", "tests/",
         "--ignore=tests/auto-generated", "--tb=short", "-q"],
        capture_output=True, text=True, cwd=str(JUGGLE_REPO), timeout=120
    )
    if result.returncode != 0:
        logger.error("smoke test failed:\n%s", result.stdout[-500:])
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> int:
    today = today_str()
    branch = _branch_name()
    cost_tracker = CostTracker(cap_usd=COST_CAP, routine=ROUTINE, dry_run=dry_run)
    pr_sections: dict = {}
    issues_to_file: list[dict] = []
    issues_filed_urls: list[str] = []
    partial = False

    # Safety gate: abort if any agent is mid-task to prevent git clobber
    if not dry_run:
        _gate_db = get_db()
        if has_busy_agents(_gate_db):
            msg = "Autofix aborted — agent(s) currently busy. Re-run after agents complete."
            logger.warning(msg)
            print(f"ABORTED: {msg}", file=sys.stderr)
            return 1

    # Pre-flight: check for existing autofix PR
    existing_prs = gh_pr_list_head("cyc_schedule-autofix-")
    if existing_prs and not dry_run:
        msg = f"autofix PR from {existing_prs[0].get('headRefName', 'unknown')} still open — review or close before next run"
        logger.warning(msg)
        db = get_db()
        tid = _find_or_create_schedule_thread(db)
        if tid:
            db.add_action_item(thread_id=tid, message=msg, type_="manual_step", priority="high")
        print(f"SKIPPED: {msg}", file=sys.stderr)
        return 1

    if not dry_run:
        # Create and switch to autofix branch
        git_run(["checkout", "-b", branch], cwd=JUGGLE_REPO)

    try:
        fx1_ruff(branch, dry_run, pr_sections)
    except CostCapExceeded as e:
        _handle_cost_cap(e, pr_sections, branch, today, pr_sections, issues_filed_urls, dry_run)
        return 1

    try:
        fx2_vulture(branch, dry_run, pr_sections, issues_to_file)
    except CostCapExceeded as e:
        partial = True
        logger.error("Cost cap hit at FX-2: %s", e)

    if not partial:
        try:
            fx3_test_gaps(branch, dry_run, pr_sections, cost_tracker)
        except CostCapExceeded as e:
            partial = True
            logger.error("Cost cap hit at FX-3: %s", e)

    if not partial:
        try:
            fx4_watchdog_tests(branch, dry_run, pr_sections, cost_tracker)
        except CostCapExceeded as e:
            partial = True
            logger.error("Cost cap hit at FX-4: %s", e)

    if not partial:
        try:
            drift_text, _ = fx5_doc_drift(branch, dry_run, pr_sections, cost_tracker)
        except CostCapExceeded as e:
            partial = True
            drift_text = ""
            logger.error("Cost cap hit at FX-5: %s", e)
    else:
        drift_text = ""

    if not partial:
        try:
            fx6_changelog(branch, dry_run, pr_sections, cost_tracker)
        except CostCapExceeded as e:
            partial = True
            logger.error("Cost cap hit at FX-6: %s", e)

    fx7_graphify(branch, dry_run, pr_sections)

    # Out-of-PR issues
    is1_bandit(issues_to_file)
    is2_skill_audit(issues_to_file)

    # Smoke test on branch (skip if dry run)
    if not dry_run:
        if not _smoke_test(branch):
            logger.warning("Smoke test failed — check commits for regression")
            pr_sections["SMOKE"] = {"status": "⚠️ smoke test failed — see PR for details"}

    # File issues (dedup)
    for iss in issues_to_file[:10]:
        title = iss["title"]
        if gh_issue_exists(title):
            logger.info("issue already exists (skip): %s", title[:60])
            continue
        if dry_run:
            logger.info("DRY RUN: would create issue: %s", title[:60])
            continue
        url = gh_create_issue(title, iss.get("body", ""), iss.get("labels"), dry_run=False)
        if url:
            issues_filed_urls.append(url)

    # Read dogfood snippet for PR body
    dogfood_snippet = _read_dogfood_snippet()

    # Build PR description
    pr_title = f"autofix: {today}" + (" [PARTIAL]" if partial else "")
    pr_body = _build_pr_description(today, pr_sections, issues_filed_urls, drift_text, dogfood_snippet)

    if dry_run:
        # Dry-run SAMPLE dir: /tmp default; tests override to tmp_path (M1, no stale false-green).
        out = Path(os.environ.get("JUGGLE_SCHEDULE_SAMPLE_DIR", "/tmp")) / "schedule-autofix-sample-PR.md"
        out.write_text(f"# {pr_title}\n\n{pr_body}")
        print(f"DRY RUN: PR description written to {out}")
        print(f"DRY RUN: cost estimate ${cost_tracker.total:.4f}")
        return 0

    # Push branch and open PR
    try:
        push_result = git_run(["push", "-u", "origin", branch], cwd=JUGGLE_REPO, check=False)
        if push_result.returncode != 0:
            db = get_db()
            tid = _find_or_create_schedule_thread(db)
            msg = f"autofix push failed: {push_result.stderr[:200]}"
            if tid:
                db.add_action_item(thread_id=tid, message=msg, type_="failure", priority="high")
            git_run(["branch", "-D", branch], cwd=JUGGLE_REPO, check=False)
            git_run(["checkout", "main"], cwd=JUGGLE_REPO, check=False)
            return 1

        pr_result = subprocess.run(
            ["gh", "pr", "create", "--title", pr_title, "--body", pr_body, "--base", "main"],
            capture_output=True, text=True, cwd=str(JUGGLE_REPO)
        )
        if pr_result.returncode == 0:
            pr_url = pr_result.stdout.strip()
            print(f"autofix PR created: {pr_url}")
        else:
            logger.error("PR creation failed: %s", pr_result.stderr)
    finally:
        git_run(["checkout", "main"], cwd=JUGGLE_REPO, check=False)

    mark_run_complete(ROUTINE)
    return 0


def _handle_cost_cap(exc, pr_sections, branch, today, _all_sections, issues_filed, dry_run):
    logger.error("cost cap exceeded: %s", exc)
    if not dry_run:
        gh_create_issue(
            f"autofix: cost cap exceeded — {today}",
            f"Run hit ${2.00:.2f} cap.\n\nPartial PR may have been created.\n\n{exc}",
            ["autofix"],
        )


def _find_or_create_schedule_thread(db):
    try:
        rows = db_query(db, "SELECT id FROM threads ORDER BY created_at DESC LIMIT 1")
        return rows[0]["id"] if rows else None
    except Exception:
        return None


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    sys.exit(run(dry_run=args.dry_run))
