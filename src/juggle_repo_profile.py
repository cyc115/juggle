"""juggle_repo_profile — per-repo RepoProfile resolution (Agent Prompt
Contract v2, PC1: docs/2026-07-03-agent-prompt-contract-v2-spec.md).

Owns: ``is_juggle_repo`` (marker check via .claude-plugin/plugin.json) and
``resolve_repo_profile(repo_path)`` — precedence CLAUDE.md's ``# juggle``
section (highest) -> DB/config ``repos[repo].test_cmd`` -> defaults (lowest).
Defaults are the juggle repo's own baked-in profile when ``repo_path`` is
this repo (``full_suite_cmd="juggle verify"`` — the doctor tmp-DB smoke folds
into that one command so the recipe never needs separate prompt text) or
generic ``None``s for any other repo (DA-3: explicit "not configured" beats a
guessed command). No new DB tables (DA-6): the ``repos`` dict in
juggle_settings.py IS the DB/config tier; CLAUDE.md's ``# juggle`` section is
the documented, code-free extension point for foreign repos.

Must not own: the PromptContext/RepoProfile dataclasses or the renderer
(juggle_prompt_context), VCS backend detection (vcs.py).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from juggle_prompt_context import RepoProfile
from juggle_settings import get_repo_config

_PROFILE_KEYS = ("full_suite_cmd", "smoke_cmd", "quality_gate_skill", "version_bump_policy")
_SECTION_HEADING_RE = re.compile(r"^#\s*juggle\s*$", re.IGNORECASE)
_TOP_HEADING_RE = re.compile(r"^#\s+\S")
_KEY_VALUE_RE = re.compile(
    r"^\s*(" + "|".join(_PROFILE_KEYS) + r")\s*:\s*(.+?)\s*$"
)


def is_juggle_repo(repo_path: str) -> bool:
    """True iff ``repo_path`` is a checkout of the juggle plugin itself."""
    plugin_json = Path(repo_path) / ".claude-plugin" / "plugin.json"
    if not plugin_json.is_file():
        return False
    try:
        data = json.loads(plugin_json.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("name") == "juggle"


def _juggle_repo_defaults() -> dict:
    return {
        "full_suite_cmd": "juggle verify",
        "smoke_cmd": None,
        "quality_gate_skill": "mike:pre-pr",
        "version_bump_policy": (
            "automatic in integrate (feat/fix/breaking commit prefix) — never hand-bump"
        ),
    }


def _generic_defaults() -> dict:
    return {key: None for key in _PROFILE_KEYS}


def _parse_claude_md_juggle_section(repo_path: str) -> dict:
    path = Path(repo_path) / "CLAUDE.md"
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SECTION_HEADING_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return {}
    section: dict = {}
    for line in lines[start:]:
        if _TOP_HEADING_RE.match(line):
            break
        m = _KEY_VALUE_RE.match(line)
        if m:
            section[m.group(1)] = m.group(2).strip().strip("`")
    return section


def resolve_repo_profile(repo_path: str) -> RepoProfile:
    """CLAUDE.md '# juggle' section -> DB/config -> defaults."""
    resolved = _juggle_repo_defaults() if is_juggle_repo(repo_path) else _generic_defaults()

    test_cmd = get_repo_config(repo_path).get("test_cmd")
    if test_cmd:
        resolved["full_suite_cmd"] = test_cmd

    resolved.update(
        {k: v for k, v in _parse_claude_md_juggle_section(repo_path).items() if v}
    )
    return RepoProfile(**resolved)
