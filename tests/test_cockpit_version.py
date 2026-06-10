"""Cockpit title-bar version display.

The cockpit Header sub_title must surface the juggle version sourced from
.claude-plugin/plugin.json (single source of truth), e.g. "Cockpit v2 · v1.60.1".
The version is read from plugin.json here too so the assertion never drifts.
"""

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("rich")
pytest.importorskip("textual")

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

import juggle_cockpit  # noqa: E402


def _plugin_version() -> str:
    pj = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    return json.loads(pj.read_text())["version"]


def test_get_version_matches_plugin_json():
    assert juggle_cockpit._get_version() == _plugin_version()


def test_cockpit_subtitle_contains_version():
    version = _plugin_version()
    sub = juggle_cockpit._cockpit_subtitle(version)
    assert f"v{version}" in sub


def test_cockpit_subtitle_narrow_degrades_to_version_only():
    """At narrow widths the prefix is dropped, but the version stays."""
    version = _plugin_version()
    sub = juggle_cockpit._cockpit_subtitle(version, width=80)
    assert f"v{version}" in sub
    assert "·" not in sub
