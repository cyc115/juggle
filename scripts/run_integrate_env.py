#!/usr/bin/env python3
"""Run a command under EXACTLY the environment `juggle integrate` gives the suite.

`make test-integrate` is this script. It exists so a human or agent can
reproduce integrate's verdict locally with one command, through the SAME
`juggle_integrate_env.sanitized_env()` the integrate gate uses — never a
hand-copied list of variables that can drift (2026-07-25 cyc_LI
env-divergence incident: 4 failed integrations, 8 commits blocked ~3 days,
because the suite silently saw a different environment than the operator did).

Usage:
    run_integrate_env.py <command> [args...]   run <command> under integrate's env
    run_integrate_env.py --print-cleared       list the variables it would clear
    run_integrate_env.py --print-cleared --json

Stdlib-only; safe to run with any python3 (the wrapped command does its own
`uv run` if it needs the project venv).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_integrate_env import dropped_overrides, sanitized_env  # noqa: E402


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2

    if argv[0] == "--print-cleared":
        cleared = dropped_overrides()
        if "--json" in argv:
            print(json.dumps(cleared, indent=2, sort_keys=True))
        else:
            for name in sorted(cleared):
                print(f"{name}={cleared[name]}")
        return 0

    try:
        os.execvpe(argv[0], argv, sanitized_env())
    except OSError as e:  # command not found / not executable
        print(f"run_integrate_env: cannot run {argv[0]!r}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
