"""tests/test_vcs_plugins.py — out-of-tree VCS plugin loader (SPEC §2.5.1-§2.5.2).

Covers: ``vcs_plugins.load_plugins`` scanning ``~/.juggle/vcs_plugins/<name>.py``
(good plugin, version-mismatch, broken-import), ``backend_for`` wiring a
config-bound plugin directly by ``BACKEND.name`` (resolution step 3, after
builtin detect / before cache), and the shared ``vcs_conformance`` kit run
against a plugin loaded from a real tmp file — proving the out-of-tree path
end-to-end.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402
import vcs_plugins  # noqa: E402
from vcs_types import SubmitResult, UpdateResult  # noqa: E402


GOOD_PLUGIN = textwrap.dedent(
    """
    from vcs_types import Capabilities, LandStatus, SubmitResult, UpdateResult, WorkspaceResult


    class _FakePluginBackend:
        name = "fakevcs"
        capabilities = Capabilities(async_land=True, auto_restack=False)

        def __init__(self):
            self.scripted = {}

        def repo_root(self, path):
            return self.scripted.get("repo_root") or path

        def primary_root(self, repo):
            return self.scripted.get("primary_root") or repo

        def refresh(self, repo):
            pass

        def trunk(self, repo):
            r = self.scripted.get("trunk")
            return r if r is not None else "trunk-rev"

        def resolve(self, repo, ref=None):
            r = self.scripted.get("resolve")
            return r if r is not None else (ref or "head-rev")

        def is_ancestor(self, repo, rev, of):
            return bool(self.scripted.get("is_ancestor", False))

        def create_workspace(self, repo, name, root, *, base=None):
            r = self.scripted.get("create_workspace")
            return r if r is not None else WorkspaceResult(ok=True, path=root, branch=name)

        def remove_workspace(self, repo, ws):
            return bool(self.scripted.get("remove_workspace", True))

        def current_rev(self, ws):
            r = self.scripted.get("current_rev")
            return r if r is not None else "ws-rev"

        def dirty_files(self, ws):
            return self.scripted.get("dirty_files") or []

        def has_changes(self, ws, *, since):
            return bool(self.scripted.get("has_changes", False))

        def update_to(self, ws, base):
            r = self.scripted.get("update_to")
            return r if r is not None else UpdateResult(ok=True)

        def describe_changes(self, ws, *, since):
            return self.scripted.get("describe_changes") or ""

        def submit(self, ws, *, base, mode, push=True):
            r = self.scripted.get("submit")
            return r if r is not None else SubmitResult(status="landed", landed_rev="landed-rev")

        def land_status(self, repo, ticket):
            r = self.scripted.get("land_status")
            return r if r is not None else LandStatus(state="unlanded")

        def head(self, path):
            return self.resolve(path, None)

        def is_dirty(self, path):
            return bool(self.dirty_files(path))

        def make_safety_branch(self, path, sha, name):
            return bool(self.scripted.get("make_safety_branch", True))


    BACKEND = _FakePluginBackend()
    PROTOCOL_VERSION = __PROTOCOL_VERSION__


    def detect(repo_root):
        return repo_root.endswith("fakevcs-repo")
    """
)


def _good_plugin_src(protocol_version: int) -> str:
    return GOOD_PLUGIN.replace("__PROTOCOL_VERSION__", str(protocol_version))

VERSION_MISMATCH_PLUGIN = textwrap.dedent(
    """
    class _Backend:
        name = "mismatched"

    BACKEND = _Backend()
    PROTOCOL_VERSION = 999999
    """
)

BROKEN_IMPORT_PLUGIN = textwrap.dedent(
    """
    import this_module_does_not_exist_anywhere  # noqa

    BACKEND = None
    PROTOCOL_VERSION = 1
    """
)

MISSING_BACKEND_PLUGIN = textwrap.dedent(
    """
    PROTOCOL_VERSION = 1
    """
)

MISSING_PROTOCOL_VERSION_PLUGIN = textwrap.dedent(
    """
    class _Backend:
        name = "noversion"

    BACKEND = _Backend()
    """
)


def _write_plugin(directory: Path, filename: str, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# load_plugins
# ---------------------------------------------------------------------------


def test_load_plugins_returns_empty_dict_when_dir_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=missing) == {}


def test_load_plugins_loads_good_plugin(tmp_path):
    d = tmp_path / "vcs_plugins"
    _write_plugin(
        d, "fakevcs.py", _good_plugin_src(vcs_mod.VCS_PROTOCOL_VERSION)
    )

    loaded = vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)

    assert set(loaded) == {"fakevcs"}
    backend, detect_fn = loaded["fakevcs"]
    assert backend.name == "fakevcs"
    assert isinstance(backend, vcs_mod.VCS)
    assert detect_fn("/some/fakevcs-repo") is True
    assert detect_fn("/some/other-repo") is False


def test_load_plugins_raises_on_version_mismatch(tmp_path):
    d = tmp_path / "vcs_plugins"
    _write_plugin(d, "mismatched.py", VERSION_MISMATCH_PLUGIN)

    with pytest.raises(vcs_plugins.PluginLoadError, match="PROTOCOL_VERSION"):
        vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)


def test_load_plugins_raises_on_broken_import(tmp_path):
    d = tmp_path / "vcs_plugins"
    _write_plugin(d, "broken.py", BROKEN_IMPORT_PLUGIN)

    with pytest.raises(vcs_plugins.PluginLoadError, match="import failed"):
        vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)


def test_load_plugins_raises_on_missing_backend_symbol(tmp_path):
    d = tmp_path / "vcs_plugins"
    _write_plugin(d, "nobacke.py", MISSING_BACKEND_PLUGIN)

    with pytest.raises(vcs_plugins.PluginLoadError, match="BACKEND"):
        vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)


def test_load_plugins_raises_on_missing_protocol_version_symbol(tmp_path):
    d = tmp_path / "vcs_plugins"
    _write_plugin(d, "noversion.py", MISSING_PROTOCOL_VERSION_PLUGIN)

    with pytest.raises(vcs_plugins.PluginLoadError, match="PROTOCOL_VERSION"):
        vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)


# ---------------------------------------------------------------------------
# backend_for wiring — resolution step 3 (plugin scan, after builtin detect)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches():
    vcs_mod._BACKEND_CACHE.clear()
    vcs_mod._PLUGIN_CACHE = None
    yield
    vcs_mod._BACKEND_CACHE.clear()
    vcs_mod._PLUGIN_CACHE = None


def test_backend_for_binds_directly_via_config_name(tmp_path, monkeypatch):
    d = tmp_path / "vcs_plugins"
    _write_plugin(
        d, "fakevcs.py", _good_plugin_src(vcs_mod.VCS_PROTOCOL_VERSION)
    )
    monkeypatch.setattr(vcs_plugins, "plugins_dir", lambda: d)
    repo = tmp_path / "some-repo"
    repo.mkdir()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": "fakevcs"}
    )

    backend = vcs_mod.backend_for(str(repo))

    assert backend.name == "fakevcs"


def test_backend_for_raises_on_broken_config_bound_plugin(tmp_path, monkeypatch):
    d = tmp_path / "vcs_plugins"
    _write_plugin(d, "mismatched.py", VERSION_MISMATCH_PLUGIN)
    monkeypatch.setattr(vcs_plugins, "plugins_dir", lambda: d)
    repo = tmp_path / "some-repo"
    repo.mkdir()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": "mismatched"}
    )

    with pytest.raises(vcs_plugins.PluginLoadError):
        vcs_mod.backend_for(str(repo))


def test_backend_for_scans_plugin_detect_after_builtin_miss(tmp_path, monkeypatch):
    d = tmp_path / "vcs_plugins"
    _write_plugin(
        d, "fakevcs.py", _good_plugin_src(vcs_mod.VCS_PROTOCOL_VERSION)
    )
    monkeypatch.setattr(vcs_plugins, "plugins_dir", lambda: d)
    repo = tmp_path / "fakevcs-repo"
    repo.mkdir()  # not a git/hg repo — builtin detect() misses
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": None}
    )

    backend = vcs_mod.backend_for(str(repo))

    assert backend.name == "fakevcs"


def test_backend_for_still_raises_when_no_plugin_detects(tmp_path, monkeypatch):
    d = tmp_path / "vcs_plugins"
    monkeypatch.setattr(vcs_plugins, "plugins_dir", lambda: d)  # empty dir
    repo = tmp_path / "undetectable-repo"
    repo.mkdir()
    monkeypatch.setattr(
        "juggle_settings.get_repo_config", lambda repo: {"vcs": None}
    )

    with pytest.raises(ValueError):
        vcs_mod.backend_for(str(repo))


# ---------------------------------------------------------------------------
# Conformance kit against a plugin loaded from a REAL tmp file — proves the
# out-of-tree path end-to-end (SPEC §2.5.2 done-gate for the loader itself).
# ---------------------------------------------------------------------------

from vcs_conformance import *  # noqa: F401,F403,E402


class _PluginHarness:
    """Scripted, like _FakeHarness in test_vcs_conformance.py, but the
    ``.vcs`` instance is the ACTUAL object returned by
    ``vcs_plugins.load_plugins`` against a real file on disk."""

    def __init__(self, tmp_path):
        d = tmp_path / "vcs_plugins"
        _write_plugin(
            d,
            "fakevcs.py",
            _good_plugin_src(vcs_mod.VCS_PROTOCOL_VERSION),
        )
        loaded = vcs_plugins.load_plugins(vcs_mod.VCS_PROTOCOL_VERSION, directory=d)
        self.vcs, _detect_fn = loaded["fakevcs"]
        self._n = 0

    def new_repo(self):
        return "/fake/repo"

    def new_workspace_path(self):
        return "/fake/ws"

    def _landed_effects(self, rev):
        self.vcs.scripted["current_rev"] = rev
        self.vcs.scripted["resolve"] = rev
        self.vcs.scripted["has_changes"] = True
        self.vcs.scripted["is_ancestor"] = True
        self.vcs.scripted["submit"] = SubmitResult(status="landed", landed_rev=rev)

    def commit(self, ws, name, content):
        self._n += 1
        self._landed_effects(f"rev-{self._n}")

    def dirty(self, ws, name, content):
        self.vcs.scripted["dirty_files"] = [name]

    def advance_trunk(self, repo, name, content):
        self._n += 1
        new_base = f"trunk-{self._n}"
        self.vcs.scripted["resolve"] = new_base
        self.vcs.scripted["is_ancestor"] = True
        self.vcs.scripted["update_to"] = UpdateResult(ok=True)
        return new_base

    def conflicting_advance_trunk(self, repo, name, content):
        new_base = self.advance_trunk(repo, name, content)
        self.vcs.scripted["update_to"] = UpdateResult(ok=False, conflicts=[name])
        return new_base

    def leave_interrupted_update(self, ws, new_base):
        pass


@pytest.fixture
def conformance_harness(tmp_path):
    return _PluginHarness(tmp_path)
