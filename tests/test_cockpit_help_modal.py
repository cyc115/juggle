"""TDD tests for the improved ? Help modal (COCKPIT_HELP_TABLE + build_help_content).

Verifies:
  1. Every action in CockpitApp.BINDINGS has a full description in the help table.
  2. build_help_content() returns a grouped structure with non-empty descriptions.
  3. No drift: future BINDINGS additions without table entries are caught.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("textual", reason="textual not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _all_binding_actions() -> set[str]:
    """Return the set of unique action names from CockpitApp.BINDINGS."""
    from juggle_cockpit import CockpitApp
    return {b.action for b in CockpitApp.BINDINGS}


def _help_table_actions() -> set[str]:
    """Return set of action names covered by COCKPIT_HELP_TABLE."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    return {e["action"] for group in COCKPIT_HELP_TABLE for e in group["entries"]}


# ---------------------------------------------------------------------------
# Cycle 1 — COCKPIT_HELP_TABLE structure
# ---------------------------------------------------------------------------


def test_cockpit_help_table_exists():
    """COCKPIT_HELP_TABLE is importable from juggle_cockpit_modals."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    assert isinstance(COCKPIT_HELP_TABLE, list)
    assert len(COCKPIT_HELP_TABLE) > 0


def test_cockpit_help_table_groups_have_required_keys():
    """Each group has 'group' (label) and 'entries' (list)."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    for group in COCKPIT_HELP_TABLE:
        assert "group" in group, f"Missing 'group' key in {group}"
        assert "entries" in group, f"Missing 'entries' key in {group}"
        assert isinstance(group["entries"], list)
        assert len(group["entries"]) > 0, f"Empty entries in group '{group['group']}'"


def test_cockpit_help_table_entries_have_required_keys():
    """Each entry has 'action', 'key', 'short', and 'desc' keys."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    for group in COCKPIT_HELP_TABLE:
        for entry in group["entries"]:
            for field in ("action", "key", "short", "desc"):
                assert field in entry, (
                    f"Entry in group '{group['group']}' missing field '{field}': {entry}"
                )


def test_cockpit_help_table_no_empty_descriptions():
    """Every entry has a non-empty 'desc' string."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    for group in COCKPIT_HELP_TABLE:
        for entry in group["entries"]:
            assert entry["desc"].strip(), (
                f"Empty desc in group '{group['group']}' for action '{entry['action']}'"
            )


def test_cockpit_help_table_no_empty_short_labels():
    """Every entry has a non-empty 'short' string."""
    from juggle_cockpit_modals import COCKPIT_HELP_TABLE
    for group in COCKPIT_HELP_TABLE:
        for entry in group["entries"]:
            assert entry["short"].strip(), (
                f"Empty short in group '{group['group']}' for action '{entry['action']}'"
            )


# ---------------------------------------------------------------------------
# Cycle 2 — Every BINDING action covered by the help table
# ---------------------------------------------------------------------------


def test_every_binding_action_covered_by_help_table():
    """Every unique action in CockpitApp.BINDINGS is in COCKPIT_HELP_TABLE.

    Regression pin: added 2026-06-16 to prevent future BINDINGS additions
    from silently rendering without an explanation in the Help modal.

    Known directional aliases are intentionally merged into single display rows:
    - scroll_up  → merged into scroll_down row ("j / k (↓ / ↑)")
    - page_up    → merged into page_down row ("PgDn / PgUp")
    """
    # These are directional counterparts shown in a single merged row.
    _MERGED_ALIASES = {"scroll_up", "page_up"}
    binding_actions = _all_binding_actions() - _MERGED_ALIASES
    help_actions = _help_table_actions()
    missing = binding_actions - help_actions
    assert not missing, (
        f"The following BINDINGS actions have no entry in COCKPIT_HELP_TABLE: {sorted(missing)}. "
        "Add entries to COCKPIT_HELP_TABLE in juggle_cockpit_modals.py."
    )


# ---------------------------------------------------------------------------
# Cycle 3 — build_help_content pure-function builder
# ---------------------------------------------------------------------------


def test_build_help_content_exists():
    """build_help_content is importable and callable."""
    from juggle_cockpit_modals import build_help_content
    result = build_help_content()
    assert isinstance(result, list)


def test_build_help_content_returns_groups():
    """build_help_content returns a list of group dicts with 'group' and 'entries'."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    assert len(groups) >= 3, "Expected at least 3 groups (Navigation, Thread actions, App)"
    for g in groups:
        assert "group" in g
        assert "entries" in g
        assert len(g["entries"]) > 0


def test_build_help_content_entries_have_desc():
    """Every entry returned by build_help_content has non-empty 'desc'."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    for g in groups:
        for entry in g["entries"]:
            assert entry.get("desc", "").strip(), (
                f"Entry {entry} in group '{g['group']}' has empty desc"
            )


def test_build_help_content_entries_have_key():
    """Every entry returned by build_help_content has non-empty 'key'."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    for g in groups:
        for entry in g["entries"]:
            assert entry.get("key", "").strip(), (
                f"Entry {entry} in group '{g['group']}' has empty key"
            )


def test_build_help_content_has_navigation_group():
    """build_help_content includes a Navigation group."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    group_names = [g["group"] for g in groups]
    assert any("nav" in n.lower() or "navigation" in n.lower() for n in group_names), (
        f"No Navigation group found. Got: {group_names}"
    )


def test_build_help_content_has_thread_actions_group():
    """build_help_content includes a Thread actions group."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    group_names = [g["group"] for g in groups]
    assert any("thread" in n.lower() for n in group_names), (
        f"No Thread actions group found. Got: {group_names}"
    )


def test_build_help_content_deduplicates_scroll_aliases():
    """j/k and arrow key aliases are merged — scroll_down appears once in help content."""
    from juggle_cockpit_modals import build_help_content
    groups = build_help_content()
    all_actions = [e["action"] for g in groups for e in g["entries"]]
    count = all_actions.count("scroll_down")
    assert count == 1, f"scroll_down should appear exactly once, got {count}"


# ---------------------------------------------------------------------------
# Cycle 4 — rendered output smoke check
# ---------------------------------------------------------------------------


def test_render_help_lines_contains_full_descriptions():
    """render_help_lines returns strings that include full descriptions (not just short labels)."""
    from juggle_cockpit_modals import render_help_lines
    lines = render_help_lines()
    full_text = "\n".join(lines)
    # Check some known full descriptions are present
    assert "Switch active thread" in full_text, "Missing 'Switch active thread' description"
    assert "Archive thread" in full_text, "Missing 'Archive thread' description"
    assert "Decommission agent" in full_text, "Missing 'Decommission agent' description"


def test_render_help_lines_has_group_headers():
    """render_help_lines output includes group section headers."""
    from juggle_cockpit_modals import render_help_lines
    lines = render_help_lines()
    full_text = "\n".join(lines)
    assert "Navigation" in full_text
    assert "Thread" in full_text


def test_render_help_lines_close_hint():
    """render_help_lines ends with Esc/q close hint."""
    from juggle_cockpit_modals import render_help_lines
    lines = render_help_lines()
    assert any("close" in l.lower() or "esc" in l.lower() for l in lines), (
        "Missing Esc/q close hint in rendered help lines"
    )
