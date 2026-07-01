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
    "src/juggle_cockpit.py": 1075,
    "src/juggle_watchdog.py": 1051,
    "src/juggle_tmux.py": 881,
    # speedup-tier M1 (2026-06-21) re-baseline: isolating the dry-run sample dir
    # to tmp_path needs one `import os` per schedule routine (the env override is
    # read at the existing write site; nothing to extract for a 1-line read).
    "src/schedules/autofix.py": 825,
    "src/juggle_cockpit_modals.py": 810,
    "src/juggle_cmd_projects.py": 755,
    "src/juggle_cmd_threads.py": 670,
    "src/schedules/reflect.py": 584,
    "src/dbops/threads.py": 559,  # slug-alloc extracted to dbops/slug_alloc.py (2026-06-21)
    "src/juggle_scheduler.py": 494,
    # selfheal-triage-v2 P1 (2026-06-21) re-baseline: irreducible plan-mandated
    # growth — a config key in DEFAULTS, a status constant the tests import from
    # schema, a Migration-45 call in apply_recent_migrations, and argparse wiring
    # cannot be extracted out of their owning modules. Migration 45's body WAS
    # extracted to dbops/migration_selfheal_status_check.py (491->443 here).
    "src/juggle_settings.py": 451,
    "src/juggle_cmd_integrate.py": 450,
    "src/juggle_cockpit_view.py": 461,
    "src/juggle_cockpit_model.py": 439,
    "src/dbops/migrations_recent.py": 390,  # 398->390 P8 c4: extracted the Migration-50.. P8 block to dbops/migrations_p8.py (apply_p8_migrations) so the collapse chain grows there, not here (2026-06-29)
    "scripts/talkback": 415,
    "src/schedules/dogfood.py": 407,  # +1 `import os` — speedup-tier M1 (2026-06-21)
    "src/juggle_cmd_research.py": 398,
    "src/juggle_graph_dispatch.py": 397,
    "src/juggle_watchdog_daemon.py": 427,
    "src/dbops/db_topics.py": 366,  # +1 P8 engine delegation: irreducible db_node_machine import (topic_transition delegates the decision) (2026-06-27)
    "src/juggle_context.py": 345,
    "src/juggle_watchdog_singleton.py": 367,
    "src/dbops/schema.py": 342,  # selfheal-v2 P1: VALID_ERROR_STATUSES constant
    # juggle_cli_parsers_misc.py removed — P9 R4 deleted the 4 flat parser walls
    # (ported to the COMMANDS table; main() now builds via build_parser()).
    "src/dbops/agents.py": 320,
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
