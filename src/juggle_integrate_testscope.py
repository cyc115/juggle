#!/usr/bin/env python3
"""Pure, side-effect-free test scoping for juggle integrate.

select_scoped_tests: maps branch-changed files → relevant test paths.
build_test_command:  splices scoped paths into the base test command.
"""

import fnmatch
import re
from pathlib import Path, PurePosixPath


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py")
    )


def _posix(path: str) -> str:
    """Normalise path to forward-slash form (handles Windows-style separators)."""
    return path.replace("\\", "/")


def select_scoped_tests(
    changed_files: list[str],
    existing_test_files: set[str],
    core_globs: list[str] | None = None,
) -> dict:
    """Map changed files to the minimal set of test files that covers them.

    Returns {"mode": "scoped"|"full"|"skip", "paths": list[str], "reason": str}.

    Rules (applied in order):
    1. Normalize to posix repo-relative paths.
    2. Collect target tests:
       a. Changed test files (tests/**) included directly (even new ones).
       b. Non-test *.py → derive stem → find test_<stem>.py anywhere in existing.
       c. Union core_globs (fnmatch) from existing_test_files.
    3. Decide mode:
       - No *.py changed → skip.
       - Unmapped non-test src *.py (no matching test) → full (safety fallback).
       - Otherwise → scoped.
    """
    core_globs = core_globs or []
    norm = [_posix(f) for f in changed_files]

    py_changed = [f for f in norm if f.endswith(".py")]
    if not py_changed:
        return {"mode": "skip", "paths": [], "reason": "no python changes"}

    collected: set[str] = set()
    unmapped_src: list[str] = []

    for f in py_changed:
        basename = PurePosixPath(f).name
        if _is_test_file(basename):
            # Rule a: changed test file → include directly
            collected.add(f)
        else:
            # Rule b: map src/<...>/foo.py → test_foo.py (any tests/ subdir)
            stem = Path(basename).stem
            test_name = f"test_{stem}.py"
            matches = {t for t in existing_test_files if PurePosixPath(t).name == test_name}
            if matches:
                collected.update(matches)
            else:
                unmapped_src.append(f)

    if unmapped_src:
        reason = f"unmapped source change {unmapped_src[0]} — full suite for safety"
        return {"mode": "full", "paths": [], "reason": reason}

    # Rule c: core_globs union
    for pattern in core_globs:
        collected.update(t for t in existing_test_files if fnmatch.fnmatch(t, pattern))

    paths = sorted(collected)
    n = len(paths)
    return {"mode": "scoped", "paths": paths, "reason": f"{n} mapped test file(s)"}


def build_test_command(base_test_cmd: str, paths: list[str]) -> str:
    """Append scoped paths to base_test_cmd, stripping any trailing path args.

    Keeps runner + flags (e.g. -q, -m '...'), drops bare path tokens at the end.
    """
    if not paths:
        return base_test_cmd

    tokens = base_test_cmd.split()
    # Strip trailing bare path-like tokens (no leading '-', contains '/' or ends with '/')
    while tokens:
        last = tokens[-1]
        if last.startswith("-"):
            break
        # Quoted -m argument values are kept by not touching flag-prefixed tokens.
        # A token is a "path arg" if it looks like a path segment.
        if re.search(r"[/\\.]", last) or last.endswith("/"):
            tokens.pop()
        else:
            break

    return " ".join(tokens + paths)
