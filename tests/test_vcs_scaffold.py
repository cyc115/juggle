"""tests/test_vcs_scaffold.py — scaffolding a bundled recipe into a loadable
out-of-tree plugin (vcs-backend-init step 2, recipe tier).

Isolation: these tests write into a tmp plugins dir (never ~/.juggle) via
``_JUGGLE_VCS_PLUGINS_DIR`` and reset ``vcs._PLUGIN_CACHE`` so a scaffolded
recipe never leaks into the real plugin scan (per the recon gotcha).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402
import vcs_plugins  # noqa: E402
from juggle_vcs_scaffold import RECIPES, scaffold  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_plugins(tmp_path, monkeypatch):
    d = tmp_path / "vcs_plugins"
    monkeypatch.setenv("_JUGGLE_VCS_PLUGINS_DIR", str(d))
    vcs_mod._PLUGIN_CACHE = None
    vcs_mod._BACKEND_CACHE.clear()
    yield
    vcs_mod._PLUGIN_CACHE = None
    vcs_mod._BACKEND_CACHE.clear()


def test_scaffold_toy_yields_loadable_plugin(tmp_path):
    dest = tmp_path / "vcs_plugins"
    path = scaffold("toy", dest_dir=dest)

    assert path == dest / "toy.py"
    loaded = vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=dest)
    backend, detect_fn = loaded["toy"]
    assert backend.name == "toy"
    assert isinstance(backend, vcs_mod.VCS)  # implements all 15 Protocol methods


def test_scaffold_sapling_loads_without_sl_installed(tmp_path):
    dest = tmp_path / "vcs_plugins"
    scaffold("sapling", dest_dir=dest)

    loaded = vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=dest)
    backend, _detect = loaded["sapling"]
    assert backend.name == "sapling"
    # The motivating async-land case: submit-for-review, poll-to-land.
    assert backend.capabilities.async_land is True
    assert isinstance(backend, vcs_mod.VCS)


def test_scaffold_custom_name(tmp_path):
    dest = tmp_path / "vcs_plugins"
    path = scaffold("toy", name="myvcs", dest_dir=dest)
    assert path == dest / "myvcs.py"
    assert path.exists()


def test_scaffold_unknown_recipe_is_fail_loud(tmp_path):
    with pytest.raises(ValueError, match="unknown recipe"):
        scaffold("no-such-vcs", dest_dir=tmp_path / "vcs_plugins")


def test_git_is_not_a_scaffoldable_recipe():
    # git is the in-tree builtin — there is nothing to scaffold.
    assert "git" not in RECIPES
