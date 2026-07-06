"""juggle_cmd_vcs — handlers for the ``juggle vcs`` group (the deterministic CLI
that ``/juggle:vcs-backend-init`` orchestrates).

Subcommands: ``detect`` (step 1), ``scaffold`` (step 2), ``configure`` (step 3),
``conformance`` (step 4 gate), and ``init`` (the full flow). Logic lives here in
code, not the .md command (code-over-prompts): behavior is deterministic and
testable. ``conformance``/``init`` are FAIL-LOUD — a red conformance run exits
non-zero and the readiness checklist is never "ready" on red.

Must not own: argparse wiring (juggle_cli_parsers_vcs) or any step's internals
(detect/scaffold/configure/validate live in their own modules).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from juggle_repo_vcs import repo_trunk, write_repo_vcs_config
from juggle_vcs_detect import detect_repo
from juggle_vcs_harness import HARNESSES
from juggle_vcs_scaffold import scaffold
from juggle_vcs_validate import ReadinessItem, ReadinessReport, run_conformance


def _repo(args) -> str:
    return os.path.abspath(getattr(args, "repo", None) or os.getcwd())


def cmd_vcs_detect(args) -> None:
    d = detect_repo(_repo(args))
    if getattr(args, "json_out", False):
        print(json.dumps(d.__dict__, indent=2))
        return
    print(f"vcs:        {d.vcs or '(unknown)'}")
    print(f"trunk:      {d.trunk}")
    print(f"workflow:   {d.workflow}")
    print(f"async_land: {d.async_land}")
    tier = "builtin" if d.recipe is None else d.recipe
    print(f"resolve:    {tier}")


def cmd_vcs_scaffold(args) -> None:
    dest = getattr(args, "dir", None)
    path = scaffold(args.recipe, name=getattr(args, "name", None),
                    dest_dir=Path(dest) if dest else None)
    print(f"scaffolded {args.recipe} recipe -> {path}")


def cmd_vcs_configure(args) -> None:
    repo = _repo(args)
    write_repo_vcs_config(
        repo,
        vcs=args.vcs,
        trunk=args.trunk,
        async_land=_as_bool(args.async_land),
        submit_cmd=getattr(args, "submit_cmd", None),
        land_status_cmd=getattr(args, "land_status_cmd", None),
    )
    print(f"configured repo {repo} -> vcs={args.vcs} trunk={args.trunk}")


def _run_bundled_conformance(backend_name: str):
    """Run the shared kit against a bundled harness (git|toy) in a throwaway
    dir. Returns a ConformanceReport, or None if no bundled harness exists."""
    if backend_name not in HARNESSES:
        return None
    base = tempfile.mkdtemp(prefix="juggle-vcs-conf-")
    try:
        return run_conformance(HARNESSES[backend_name](base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def cmd_vcs_conformance(args) -> None:
    name = args.backend
    report = _run_bundled_conformance(name)
    if report is None:
        print(
            f"backend '{name}': no bundled conformance harness — behavioral "
            "conformance must run in the plugin repo against the real VCS "
            "(docs/create-your-own-vcs-backend.md §4)."
        )
        return
    for tname, ok, err in report.results:
        if not ok:
            print(f"  ❌ {tname}: {err}")
    print(f"conformance {name}: {report.passed}/{report.total} passed")
    if not report.all_green:
        sys.exit(1)  # fail-loud — never green on a red kit run


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def cmd_vcs_init(args) -> None:
    """The full flow: detect -> (scaffold recipe) -> configure -> conformance ->
    readiness checklist. Exits non-zero unless the checklist is fully green."""
    repo = _repo(args)
    d = detect_repo(repo)
    recipe = getattr(args, "recipe", None) or d.recipe
    vcs_name = getattr(args, "vcs", None) or d.vcs

    if recipe == "agentic" or (vcs_name is None and recipe is None):
        print(
            "Unknown VCS — no builtin and no bundled recipe. Seed a coder agent "
            "with docs/create-your-own-vcs-backend.md to implement the 15-method "
            "plugin, then re-run `juggle vcs init --recipe <name>`."
        )
        sys.exit(1)

    items: list[ReadinessItem] = [
        ReadinessItem(f"VCS detected: {vcs_name or recipe}", True)
    ]

    # step 2 — scaffold a recipe plugin (git needs none).
    if recipe not in (None, "agentic"):
        try:
            path = scaffold(recipe, name=vcs_name)
            items.append(ReadinessItem(f"recipe scaffolded: {path}", True))
        except Exception as exc:
            items.append(ReadinessItem("recipe scaffolded", False, repr(exc)))
        import vcs as vcs_mod
        vcs_mod._PLUGIN_CACHE = None  # pick up the freshly-written plugin

    # step 3 — configure.
    write_repo_vcs_config(
        repo, vcs=vcs_name, trunk=d.trunk, async_land=d.async_land,
        submit_cmd=getattr(args, "submit_cmd", None),
        land_status_cmd=getattr(args, "land_status_cmd", None),
    )
    items.append(ReadinessItem(f"config written: vcs={vcs_name} trunk={d.trunk}", True))

    # backend binds through the single resolution funnel.
    try:
        import vcs as vcs_mod
        vcs_mod._BACKEND_CACHE.clear()
        bound = vcs_mod.backend_for(repo)
        items.append(ReadinessItem(f"backend binds: {bound.name}", bound.name == vcs_name))
    except Exception as exc:
        items.append(ReadinessItem("backend binds", False, repr(exc)))

    items.append(ReadinessItem(f"trunk resolves: {repo_trunk(repo)}", bool(repo_trunk(repo))))

    # step 4 — conformance (THE gate). Bundled backends run behaviorally; others
    # are shape-only here and must be verified in the plugin repo against live VCS.
    report = _run_bundled_conformance(vcs_name)
    if report is None:
        items.append(ReadinessItem(
            "conformance", True,
            "shape-only — run the kit against the real VCS in your plugin repo",
        ))
    else:
        items.append(ReadinessItem(
            f"conformance green ({report.passed}/{report.total})", report.all_green,
            "" if report.all_green else f"{len(report.failures())} failed",
        ))

    report_obj = ReadinessReport(items=items)
    print(f"\nReadiness — {repo}")
    for line in report_obj.render():
        print(line)
    if report_obj.ready:
        print("\n✅ juggle is configured for this repo.")
    else:
        print("\n❌ not ready — resolve the ❌ items above.")
        sys.exit(1)
