"""vcs_plugins — out-of-tree VCS backend plugin loader (SPEC §2.5.1-§2.5.2).

Loads ``~/.juggle/vcs_plugins/<name>.py`` modules via
``importlib.util.spec_from_file_location``. Each module must define two
required top-level symbols — ``BACKEND`` (a VCS-Protocol-shaped instance) and
``PROTOCOL_VERSION`` (must equal ``vcs.VCS_PROTOCOL_VERSION``) — plus an
optional ``detect(repo_root) -> bool``.

Fail-loud (§2.5.1 rationale): a missing symbol, a version mismatch, or an
import/exec error raises ``PluginLoadError`` rather than being skipped. A
config-named plugin (``vcs: "<name>"``) that fails to load must never
silently fall back to git — that would corrupt a non-git repo.

Must not own: ``backend_for``'s resolution order (``vcs.py`` owns the funnel)
or any git/hg mechanics.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


class PluginLoadError(RuntimeError):
    """A ``vcs_plugins/<name>.py`` module is malformed or its
    ``PROTOCOL_VERSION`` doesn't match juggle's — a hard failure, never a
    silent skip (docs/create-your-own-vcs-backend.md §2)."""


def plugins_dir() -> Path:
    """``~/.juggle/vcs_plugins`` — overridable via ``_JUGGLE_VCS_PLUGINS_DIR``
    for test isolation (mirrors ``_JUGGLE_CONFIG_PATH``)."""
    override = os.environ.get("_JUGGLE_VCS_PLUGINS_DIR")
    return Path(override) if override else Path.home() / ".juggle" / "vcs_plugins"


def _load_one(path: Path, protocol_version: int):
    spec = importlib.util.spec_from_file_location(f"vcs_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"vcs plugin {path}: could not build an import spec")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # fail-loud — any import/exec error is a hard failure
        raise PluginLoadError(f"vcs plugin {path}: import failed: {exc}") from exc

    if not hasattr(module, "BACKEND"):
        raise PluginLoadError(f"vcs plugin {path}: missing required 'BACKEND' symbol")
    if not hasattr(module, "PROTOCOL_VERSION"):
        raise PluginLoadError(
            f"vcs plugin {path}: missing required 'PROTOCOL_VERSION' symbol"
        )

    plugin_version = module.PROTOCOL_VERSION
    if plugin_version != protocol_version:
        raise PluginLoadError(
            f"vcs plugin {path}: PROTOCOL_VERSION {plugin_version!r} does not match "
            f"juggle's VCS_PROTOCOL_VERSION {protocol_version!r}"
        )

    return module.BACKEND, getattr(module, "detect", None)


def load_plugins(protocol_version: int, directory: Path | None = None) -> dict:
    """Scan ``directory`` (default: :func:`plugins_dir`) and return
    ``{BACKEND.name: (backend, detect_fn_or_None)}``. Missing directory scans
    as empty. Any malformed plugin file raises ``PluginLoadError`` — the scan
    is fail-loud, never a partial/silent result."""
    d = directory if directory is not None else plugins_dir()
    if not d.exists():
        return {}
    loaded = {}
    for path in sorted(d.glob("*.py")):
        backend, detect_fn = _load_one(path, protocol_version)
        loaded[backend.name] = (backend, detect_fn)
    return loaded
