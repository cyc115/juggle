"""juggle_cli_parsers_vcs — argparse wiring for the ``vcs <verb>`` group
(``/juggle:vcs-backend-init``). Registers detect/scaffold/configure/conformance/
init; handlers live in juggle_cmd_vcs (code-over-prompts).
"""
from __future__ import annotations

from juggle_cmd_vcs import (
    cmd_vcs_conformance,
    cmd_vcs_configure,
    cmd_vcs_detect,
    cmd_vcs_init,
    cmd_vcs_scaffold,
)


def register_vcs_parsers(subparsers) -> None:
    """Register the ``vcs <verb>`` subcommand group on ``subparsers``."""
    p_vcs = subparsers.add_parser("vcs", help="Set up a VCS backend for a repo")
    sub = p_vcs.add_subparsers(dest="vcs_command", required=True)

    p_detect = sub.add_parser("detect", help="Detect repo VCS, trunk, workflow")
    p_detect.add_argument("--repo", help="Repo path (default: cwd)")
    p_detect.add_argument("--json", dest="json_out", action="store_true")
    p_detect.set_defaults(func=cmd_vcs_detect)

    p_scaffold = sub.add_parser("scaffold", help="Write a bundled recipe as a plugin")
    p_scaffold.add_argument("recipe", help="Recipe name (toy|sapling)")
    p_scaffold.add_argument("--name", help="Plugin/backend name (default: recipe)")
    p_scaffold.add_argument("--dir", help="Plugins dir (default: ~/.juggle/vcs_plugins)")
    p_scaffold.set_defaults(func=cmd_vcs_scaffold)

    p_conf = sub.add_parser("configure", help="Write repo VCS config")
    p_conf.add_argument("--repo", help="Repo path (default: cwd)")
    p_conf.add_argument("--vcs", required=True, help="Backend name")
    p_conf.add_argument("--trunk", required=True, help="Trunk branch/ref name")
    p_conf.add_argument("--async-land", dest="async_land", default=False,
                        help="true|false — submit-for-async-land")
    p_conf.add_argument("--submit-cmd", dest="submit_cmd")
    p_conf.add_argument("--land-status-cmd", dest="land_status_cmd")
    p_conf.set_defaults(func=cmd_vcs_configure)

    p_kit = sub.add_parser("conformance", help="Run the conformance kit (the gate)")
    p_kit.add_argument("--backend", required=True, help="Backend name (git|toy|...)")
    p_kit.set_defaults(func=cmd_vcs_conformance)

    p_init = sub.add_parser("init", help="Full flow: detect->scaffold->configure->validate")
    p_init.add_argument("--repo", help="Repo path (default: cwd)")
    p_init.add_argument("--recipe", help="Force a recipe (overrides detection)")
    p_init.add_argument("--vcs", help="Force a backend name (overrides detection)")
    p_init.add_argument("--submit-cmd", dest="submit_cmd")
    p_init.add_argument("--land-status-cmd", dest="land_status_cmd")
    p_init.set_defaults(func=cmd_vcs_init)
