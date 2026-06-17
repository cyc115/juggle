#!/usr/bin/env python3
"""
Git merge driver for .claude-plugin/plugin.json.

Auto-resolves version-only conflicts by taking the max semver across base/ours/theirs.
Exits 1 (leaving conflict for humans) when any non-version field genuinely differs.

Usage (configured via git config merge.juggle-version.driver):
    git-merge-plugin-version.py %O %A %B
        %O = base (ancestor) path
        %A = ours (current branch) path  — WRITE merged result here
        %B = theirs (incoming) path
"""
import json
import sys


def _semver(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <base> <ours> <theirs>", file=sys.stderr)
        return 2

    base_path, ours_path, theirs_path = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        with open(base_path) as f:
            base = json.load(f)
        with open(ours_path) as f:
            ours = json.load(f)
        with open(theirs_path) as f:
            theirs = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error reading JSON: {e}", file=sys.stderr)
        return 1

    # Check for non-version conflicts: any key that differs between ours and theirs
    # (excluding "version" which we handle ourselves).
    # Conflict only when both sides have the key with different values.
    # A key present in only one side was added/removed by that side — not a conflict.
    shared_keys = set(ours) & set(theirs)
    for key in shared_keys:
        if key == "version":
            continue
        if ours[key] != theirs[key]:
            print(
                f"conflict: non-version field '{key}' differs between ours and theirs",
                file=sys.stderr,
            )
            return 1

    # Auto-resolve: take the max semver among all three.
    versions = [base.get("version", "0.0.0"), ours.get("version", "0.0.0"), theirs.get("version", "0.0.0")]
    max_version = max(versions, key=_semver)

    result = dict(ours)
    result["version"] = max_version

    with open(ours_path, "w") as f:
        json.dump(result, f, indent=2)
        f.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
