#!/usr/bin/env python3
"""Pure, side-effect-free test scoping for juggle integrate.

select_scoped_tests: maps branch-changed files → relevant test paths (pure).
build_import_index:  scans test files for imports → module_stem→{test_paths} (impure).
build_test_command:  splices scoped paths into the base test command (pure).
apply_quarantine:    prepends --deselect flags for known-red tests (pure).

Quarantine note: integrate.quarantine_tests (default: loc_gate + data_migration)
excludes pre-existing RED tests from both scoped and full runs. Shrinks as each
test is fixed (loc_gate awaits dbops refactor + budget-lower; data_migration
awaits triage). NOT permanent.
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


# ── import index builder (impure — reads files) ───────────────────────────────

# Matches:  import foo_bar
#           from foo_bar import ...
#           import pkg.mod [as ...]  → key "mod"
#           from pkg.mod import ...  → key "mod"
_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+"     # leading indent allowed (lazy/function-body imports)
    r"(?:[\w]+\.)?"               # optional pkg prefix (one level)
    r"([\w]+)"                    # the module stem we care about
    r"(?:\s+import|\s+as\s|\s*$)",
    re.MULTILINE,
)


def build_import_index(test_dir: Path) -> dict[str, set[str]]:
    """Scan test_dir for test_*.py files and build {module_stem: {repo-rel paths}}.

    Only test files (test_*.py / *_test.py) are scanned — helper modules ignored.
    Paths in the returned sets are relative to test_dir's parent (repo root).
    """
    test_dir = Path(test_dir)
    repo_root = test_dir.parent
    index: dict[str, set[str]] = {}
    for tf in test_dir.rglob("*.py"):
        if not _is_test_file(tf.name):
            continue
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(tf.relative_to(repo_root)).replace("\\", "/")
        for m in _IMPORT_RE.finditer(text):
            stem = m.group(1)
            index.setdefault(stem, set()).add(rel)
    return index


# ── pure selector ─────────────────────────────────────────────────────────────

def select_scoped_tests(
    changed_files: list[str],
    existing_test_files: set[str],
    import_index: dict[str, set[str]] | None = None,
    core_globs: list[str] | None = None,
) -> dict:
    """Map changed files to the minimal set of test files that covers them.

    Returns {"mode": "scoped"|"full"|"skip", "paths": list[str], "reason": str}.

    Mapping priority per changed non-test *.py (e.g. src/juggle_cockpit.py):
      a. Test files that import the module stem (from import_index) — primary.
      b. existing_test_files matching test_<stem>.py anywhere — secondary.
      c. If BOTH empty for any non-test src file → mode "full" (safety fallback).

    Changed test files are always included directly.
    Docs/config-only changes → mode "skip".
    """
    core_globs = core_globs or []
    import_index = import_index or {}
    norm = [_posix(f) for f in changed_files]

    py_changed = [f for f in norm if f.endswith(".py")]
    if not py_changed:
        return {"mode": "skip", "paths": [], "reason": "no python changes"}

    collected: set[str] = set()
    unmapped_src: list[str] = []

    for f in py_changed:
        basename = PurePosixPath(f).name
        if _is_test_file(basename):
            # Changed test file → include directly
            collected.add(f)
        else:
            stem = Path(basename).stem
            # (a) primary: import-reference mapping
            by_import = import_index.get(stem, set())
            # (b) secondary: name-stem match
            test_name = f"test_{stem}.py"
            by_name = {t for t in existing_test_files if PurePosixPath(t).name == test_name}

            found = by_import | by_name
            if found:
                collected.update(found)
            else:
                unmapped_src.append(f)

    if unmapped_src:
        reason = f"unmapped source change {unmapped_src[0]} — full suite for safety"
        return {"mode": "full", "paths": [], "reason": reason}

    # core_globs union (fnmatch against existing_test_files)
    for pattern in core_globs:
        collected.update(t for t in existing_test_files if fnmatch.fnmatch(t, pattern))

    paths = sorted(collected)
    n = len(paths)
    return {"mode": "scoped", "paths": paths, "reason": f"{n} mapped test file(s)"}


# ── pure helpers ──────────────────────────────────────────────────────────────

def build_test_command(base_test_cmd: str, paths: list[str]) -> str:
    """Append scoped paths to base_test_cmd, stripping any trailing path args.

    Keeps runner + flags (e.g. -q, -m '...'), drops bare path tokens at the end.
    """
    if not paths:
        return base_test_cmd

    tokens = base_test_cmd.split()
    while tokens:
        last = tokens[-1]
        if last.startswith("-"):
            break
        if re.search(r"[/\\.]", last) or last.endswith("/"):
            tokens.pop()
        else:
            break

    return " ".join(tokens + paths)


def apply_quarantine(cmd: str, quarantine: list[str]) -> str:
    """Prepend --deselect <path> flags for each quarantined test.

    Flags are inserted before any positional path arguments so pytest sees them
    in the right position. Empty quarantine returns cmd unchanged.
    """
    if not quarantine:
        return cmd

    tokens = cmd.split()
    # Find split point: last flag token index (flags start with '-').
    # Always at least 1 so we never insert --deselect before the executable.
    split = 1 if tokens else 0
    for i, tok in enumerate(tokens):
        if tok.startswith("-"):
            split = i + 1

    deselect_flags: list[str] = []
    for path in quarantine:
        deselect_flags += ["--deselect", path]

    result = tokens[:split] + deselect_flags + tokens[split:]
    return " ".join(result)
