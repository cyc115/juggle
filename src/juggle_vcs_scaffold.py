"""juggle_vcs_scaffold — step 2 (recipe tier) of ``/juggle:vcs-backend-init``:
copy a bundled recipe into a loadable out-of-tree plugin.

A recipe is a complete, standalone VCS plugin module shipped under
``juggle_vcs_recipes/`` (``BACKEND`` / ``PROTOCOL_VERSION`` / optional
``detect``). ``scaffold`` copies the chosen recipe VERBATIM to
``<plugins_dir>/<name>.py`` so juggle's loader (vcs_plugins) can pick it up — no
edits to the juggle repo. Git is the in-tree builtin and is deliberately NOT a
recipe (nothing to scaffold); an unknown VCS is the agentic fallback, not a
recipe either.

Must not own: config writing (juggle_vcs_configure) or detection
(juggle_vcs_detect).
"""
from __future__ import annotations

from pathlib import Path

import juggle_vcs_recipes

# Bundled recipes: name -> source filename under juggle_vcs_recipes/.
RECIPES: dict[str, str] = {
    "toy": "toy.py",
    "sapling": "sapling.py",
}


def _recipes_dir() -> Path:
    return Path(juggle_vcs_recipes.__file__).parent


def scaffold(recipe: str, *, name: str | None = None, dest_dir: Path | None = None) -> Path:
    """Copy bundled ``recipe`` to ``<dest_dir>/<name>.py`` and return the path.

    ``name`` defaults to the recipe name (the plugin's ``BACKEND.name``);
    ``dest_dir`` defaults to the live plugins dir (``vcs_plugins.plugins_dir``).
    Fail-loud (``ValueError``) on an unknown recipe — never silently no-op."""
    if recipe not in RECIPES:
        raise ValueError(
            f"unknown recipe {recipe!r} — bundled recipes: {sorted(RECIPES)} "
            "(git is the in-tree builtin; an unlisted VCS uses the agentic path)"
        )
    if dest_dir is None:
        from vcs_plugins import plugins_dir

        dest_dir = plugins_dir()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    src = _recipes_dir() / RECIPES[recipe]
    out = dest_dir / f"{name or recipe}.py"
    out.write_text(src.read_text())
    return out
