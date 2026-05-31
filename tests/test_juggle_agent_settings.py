"""Tests for juggle_agent_settings.py — per-role Claude Code settings overlays.

The overlay must be PURELY ADDITIVE: it carries only role denials (and any
configured per-role keys), so that when launched via `--settings <file>` it
layers over the host's settings hierarchy without replacing it. Omitted keys
must NOT appear in the overlay — that is what lets the host's own settings
survive (portability across dev environments).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import juggle_agent_settings as jas


@pytest.fixture
def fake_settings(monkeypatch, tmp_path):
    """Inject a controlled settings dict into juggle_agent_settings.get_settings."""

    def _make(overlay_base=None, overlay_by_role=None):
        settings = {
            "paths": {"config_dir": str(tmp_path)},
            "agent": {
                "disallowed_tools_universal": ["mcp__opentabs__*", "Agent"],
                "disallowed_tools_by_role": {
                    "coder": ["NotebookEdit"],
                    "planner": ["Edit", "NotebookEdit"],
                    "researcher": ["Edit"],
                },
                "settings_overlay_base": overlay_base or {},
                "settings_overlay_by_role": overlay_by_role
                or {"coder": {}, "planner": {}, "researcher": {}},
            },
        }
        monkeypatch.setattr(jas, "get_settings", lambda: settings)
        return settings

    return _make


def test_overlay_deny_is_universal_plus_role(fake_settings):
    fake_settings()
    overlay = jas.build_agent_overlay("coder")
    assert overlay["permissions"]["deny"] == ["mcp__opentabs__*", "Agent", "NotebookEdit"]


def test_overlay_dedups_deny(fake_settings):
    # role deny repeats a universal entry → no duplicate in result
    fake_settings(
        overlay_by_role={"coder": {}, "planner": {}, "researcher": {}},
    )
    # planner repeats "NotebookEdit" only in its own list; universal has none here
    overlay = jas.build_agent_overlay("planner")
    deny = overlay["permissions"]["deny"]
    assert deny == ["mcp__opentabs__*", "Agent", "Edit", "NotebookEdit"]
    assert len(deny) == len(set(deny))


def test_overlay_is_additive_only_no_stray_keys(fake_settings):
    """With empty overlay config the overlay must contain ONLY permissions —
    no model/defaultMode/etc. — so host values for those keys are preserved."""
    fake_settings()
    overlay = jas.build_agent_overlay("coder")
    assert set(overlay.keys()) == {"permissions"}
    assert set(overlay["permissions"].keys()) == {"deny"}


def test_per_role_divergence(fake_settings):
    fake_settings(
        overlay_by_role={
            "coder": {"model": "claude-opus-4-8"},
            "planner": {},
            "researcher": {},
        }
    )
    coder = jas.build_agent_overlay("coder")
    planner = jas.build_agent_overlay("planner")
    assert coder["model"] == "claude-opus-4-8"
    assert "model" not in planner  # divergence: only coder overridden


def test_overlay_base_applies_to_all_roles(fake_settings):
    fake_settings(overlay_base={"env": {"FOO": "bar"}})
    for role in ("coder", "planner", "researcher"):
        assert jas.build_agent_overlay(role)["env"] == {"FOO": "bar"}


def test_overlay_base_deny_unions_with_role_deny(fake_settings):
    fake_settings(overlay_base={"permissions": {"deny": ["WebFetch"]}})
    deny = jas.build_agent_overlay("coder")["permissions"]["deny"]
    # base deny first, then universal + role, deduped
    assert deny[0] == "WebFetch"
    assert "mcp__opentabs__*" in deny and "NotebookEdit" in deny


def test_per_dispatch_overrides_merge_last(fake_settings):
    fake_settings()
    overlay = jas.build_agent_overlay(
        "coder",
        overrides={"permissions": {"additionalDirectories": ["/srv/app"]}},
    )
    assert overlay["permissions"]["additionalDirectories"] == ["/srv/app"]
    # deny still present (deep-merge, not replace)
    assert "NotebookEdit" in overlay["permissions"]["deny"]


def test_roleless_overlay_has_universal_only(fake_settings):
    fake_settings()
    overlay = jas.build_agent_overlay(None)
    assert overlay["permissions"]["deny"] == ["mcp__opentabs__*", "Agent"]


def test_write_overlay_round_trips(fake_settings, tmp_path):
    fake_settings()
    path = jas.write_agent_overlay("coder")
    assert path == tmp_path / "agent-settings" / "coder.json"
    data = json.loads(path.read_text())
    assert data["permissions"]["deny"] == ["mcp__opentabs__*", "Agent", "NotebookEdit"]


def test_write_overlay_with_overrides_is_per_agent_file(fake_settings, tmp_path):
    fake_settings()
    p1 = jas.write_agent_overlay("coder", overrides={"model": "x"})
    p2 = jas.write_agent_overlay("coder", overrides={"model": "x"})
    # unique filenames so concurrent agents don't clobber each other
    assert p1 != p2
    assert p1.parent == tmp_path / "agent-settings"
