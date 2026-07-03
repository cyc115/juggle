"""juggle_version_bump — automated semver bump derived from merged commit prefixes.

P1 (2026-07-03, irl-prevention): the version bump moves out of agent judgment
and into the integrate pipeline itself — an agent forgetting to bump (or
picking the wrong bump type) was a recurring drain. Applied on the worktree
branch AFTER the task's diffstat snapshot (so the bump commit is never
attributed to the task's own diff) and BEFORE ``submit()``'s ff-merge+push —
by the time this runs the worktree has already been rebased onto the current
main tip (integrate step 4), so ``plugin.json`` already reflects any bump
serialized ahead of us under the repo-wide integrate lock. The resulting
main-branch history is identical to bumping after the merge: a linear ff-merge
carries the bump commit onto main unchanged.

Fail-soft throughout: a repo without ``.claude-plugin/plugin.json`` (i.e. not
opted in) or any error along the way is a silent no-op — this must never
block a landed merge.
"""

import json
import re
import subprocess
from pathlib import Path

PLUGIN_JSON_REL = ".claude-plugin/plugin.json"

_BUMP_RANK = {"patch": 1, "minor": 2, "major": 3}

# Conventional-commit header: "type(scope)!: subject" — only the leading
# token before ':' matters; scope and '!' (breaking) are optional.
_HEADER_RE = re.compile(r"^(\w+)(\([^)]*\))?(!)?:\s")


def classify_commit(message: str) -> str | None:
    """Map one commit message to a bump type, or None if it doesn't warrant one."""
    if not message:
        return None
    first_line = message.splitlines()[0].strip()
    m = _HEADER_RE.match(first_line)
    if not m:
        return None
    kind, _scope, bang = m.group(1).lower(), m.group(2), m.group(3)
    if bang or "BREAKING CHANGE" in message:
        return "major"
    if kind == "feat":
        return "minor"
    if kind == "fix":
        return "patch"
    return None


def compute_bump(messages: list[str]) -> str | None:
    """Highest-ranked bump type across ``messages``, or None if none qualify."""
    best = None
    for message in messages:
        kind = classify_commit(message)
        if kind and (best is None or _BUMP_RANK[kind] > _BUMP_RANK[best]):
            best = kind
    return best


def bump_semver(version: str, bump: str) -> str:
    parts = (version.split(".") + ["0", "0", "0"])[:3]
    major, minor, patch = (int(p) for p in parts)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _commit_messages(repo: str, since: str, until: str = "HEAD") -> list[str]:
    result = subprocess.run(
        ["git", "-C", repo, "log", f"{since}..{until}", "--format=%B%x00"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout:
        return []
    return [m for m in result.stdout.split("\x00") if m.strip()]


def apply_version_bump(repo: str, since: str, until: str = "HEAD") -> str | None:
    """Bump ``.claude-plugin/plugin.json`` version in ``repo`` and commit it,
    derived from the commit prefixes in ``(since, until]``.

    Returns the new version string, or None if the repo isn't opted in (no
    plugin.json), no commit warranted a bump, or any step failed — fail-soft,
    never raises.
    """
    try:
        plugin_path = Path(repo) / PLUGIN_JSON_REL
        if not plugin_path.exists():
            return None
        bump = compute_bump(_commit_messages(repo, since, until))
        if not bump:
            return None
        data = json.loads(plugin_path.read_text())
        new_version = bump_semver(str(data.get("version", "0.0.0")), bump)
        data["version"] = new_version
        plugin_path.write_text(json.dumps(data, indent=2) + "\n")
        subprocess.run(
            ["git", "-C", repo, "add", PLUGIN_JSON_REL],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", repo, "commit", "-m", f"chore(version): bump to {new_version} [{bump}]"],
            check=True, capture_output=True,
        )
        return new_version
    except Exception:
        return None
