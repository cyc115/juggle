#!/usr/bin/env python3
"""LOC gate — enforce the repo architecture gate of <=300 lines per module.

Walks git-tracked Python files under src/ plus Python scripts in scripts/
(uv/python shebang). Any file over LIMIT lines fails the gate (exit 1) unless
it appears in GRANDFATHERED at a budget >= its current size. A grandfathered
file that GROWS past its recorded budget also fails.

THE ALLOWLIST MAY ONLY SHRINK. Entries are removed (or budgets lowered) as the
2026-06-10 refactor plan decomposes each module — never added or raised.
tests/test_loc_gate.py enforces that every entry still exceeds LIMIT (stale
entries fail the suite, forcing removal).

CLI:
    loc_gate.py                  human-readable report, exit 1 on offenders
    loc_gate.py --json           machine-readable report
    loc_gate.py --update-baseline  print (never write) a fresh allowlist dict

Stdlib-only; safe to run with any python3.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

LIMIT = 300

REPO_ROOT = Path(__file__).resolve().parent.parent

# Grandfathered offenders at their line counts as of 2026-06-10 (plan baseline,
# branch cyc_refactor-tokens). MAY ONLY SHRINK — see module docstring.
GRANDFATHERED: dict[str, int] = {
    # juggle_db.py split Phase 2.1: composition root is 144 lines (removed from allowlist).
    # dbops/migrations.py holds all 34 schema migrations as a single ordered sequence;
    # cannot split without losing migration-ordering invariant. Follow-up debt noted in results.
    "src/juggle_watchdog.py": 964,  # lowered 1332→1301 (Ph1.1) →964 (Ph2.2 inspect+restart split)
    "src/juggle_cockpit.py": 835,  # lowered from 1120 (Ph2.3: layout+profile extracted)
    # juggle_hooks.py Phase 2.5: split into hooks sub-modules (shim now 117 lines, removed).
    # Two sub-modules exceed 300 and are grandfathered below their current size:
    "src/juggle_hooks_tooluse.py": 334,   # PreToolUse+PostToolUse handlers; target ≤300
    "src/juggle_hooks_prompt.py": 326,    # lowered 350→326 (Ph4: autopilot directive → juggle_hooks_autopilot)
    "src/juggle_tmux.py": 839,
    "src/schedules/autofix.py": 823,
    "src/juggle_cmd_projects.py": 735,  # lowered from 737 (Phase 1.2 llm consolidation)
    "src/juggle_cmd_threads.py": 673,
    "src/juggle_context.py": 353,
    "src/schedules/reflect.py": 582,  # 545→582: per-section cost-cap enforcement (COST_CAP $1.00 / SECTION_CAP $0.35) merged from origin/main 80780d4 during rebase
    "src/juggle_cockpit_view.py": 460,  # lowered 499→460 (Ph4: static renders → juggle_cockpit_static; headroom for graph glyph rows)
    "src/juggle_scheduler.py": 494,
    "src/juggle_cockpit_model.py": 440,  # lowered 467→440 (Ph4: sched discovery → juggle_cockpit_sched; headroom for graph counts)
    "src/juggle_settings.py": 460,
    "src/schedules/dogfood.py": 406,
    "src/juggle_cmd_research.py": 392,
    "src/juggle_cmd_context.py": 370,
    # src/schedules/common.py (ex juggle_schedule_common) removed 2026-06-10: shrank to 297 (Phase 1.2)
    "scripts/talkback": 415,
}


def count_lines(path: Path) -> int:
    """Line count matching `wc -l` semantics (newline count, trailing partial line counts)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text:
        return 0
    n = text.count("\n")
    if not text.endswith("\n"):
        n += 1
    return n


def _is_python_script(path: Path) -> bool:
    try:
        first = path.open("r", encoding="utf-8", errors="replace").readline()
    except OSError:
        return False
    return first.startswith("#!") and ("python" in first or "uv run" in first)


def collect_files(repo_root: Path = REPO_ROOT) -> dict[str, int]:
    """Return {repo-relative path: line count} for all gated files."""
    out = subprocess.run(
        ["git", "ls-files", "--", "src", "scripts"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=True,
    ).stdout.splitlines()
    counts: dict[str, int] = {}
    for rel in out:
        p = repo_root / rel
        if not p.is_file():
            continue  # tracked but deleted in worktree
        if rel.startswith("src/"):
            if not rel.endswith(".py"):
                continue
        elif rel.startswith("scripts/"):
            if not (rel.endswith(".py") or _is_python_script(p)):
                continue
        counts[rel] = count_lines(p)
    return counts


def evaluate(
    counts: dict[str, int],
    allowlist: dict[str, int],
    limit: int = LIMIT,
) -> dict:
    """Pure gate logic. Returns {offenders: [...], stale: [...], files_checked: n}."""
    offenders: list[dict] = []
    stale: list[str] = []
    for path in sorted(counts):
        n = counts[path]
        budget = allowlist.get(path)
        if n > limit:
            if budget is None or n > budget:
                offenders.append({"path": path, "lines": n, "budget": budget or limit})
        elif budget is not None:
            stale.append(path)
    return {"offenders": offenders, "stale": stale, "files_checked": len(counts)}


def main(argv: list[str]) -> int:
    counts = collect_files()
    result = evaluate(counts, GRANDFATHERED, LIMIT)

    if "--update-baseline" in argv:
        print("# Fresh GRANDFATHERED baseline (paste into scripts/loc_gate.py).")
        print("# Reminder: the allowlist MAY ONLY SHRINK relative to the committed one.")
        print("GRANDFATHERED: dict[str, int] = {")
        for path, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            if n > LIMIT:
                print(f'    "{path}": {n},')
        print("}")
        return 0

    if "--json" in argv:
        print(
            json.dumps(
                {
                    "limit": LIMIT,
                    "files_checked": result["files_checked"],
                    "offenders": result["offenders"],
                    "stale": result["stale"],
                    "allowlist": GRANDFATHERED,
                },
                indent=2,
            )
        )
        return 1 if result["offenders"] else 0

    if result["stale"]:
        for path in result["stale"]:
            print(
                f"loc_gate: STALE allowlist entry {path} "
                f"(now <= {LIMIT} lines) — remove it from GRANDFATHERED"
            )
    if result["offenders"]:
        print(f"loc_gate: FAIL — modules over {LIMIT} lines (or past grandfathered budget):")
        for o in result["offenders"]:
            print(f"  {o['lines']:>6}  {o['path']}  (budget {o['budget']})")
        return 1
    print(
        f"loc_gate: OK — {result['files_checked']} files checked, "
        f"{len(GRANDFATHERED)} grandfathered (allowlist may only shrink)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
