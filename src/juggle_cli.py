#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["rich", "httpx", "pyte", "pyyaml"]
# ///
"""
Juggle CLI - called by LLM via Bash tool for state changes.
Usage: python juggle_cli.py <command> [args]

This entry point owns env bootstrap, vault/editor helpers (test patch surface:
juggle_cli.get_settings / NVIM_SOCKET / subprocess), and main() parser wiring.
Subcommand registration lives in juggle_cli_parsers_{threads,agents,misc};
handlers live in juggle_cmd_* modules.
"""

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

# Load ~/.juggle/.env before any module-level code reads env vars.
# This makes OPENROUTER_KEY available to title_gen's Tier 1 path.
_ENV_FILE = Path.home() / ".juggle" / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# File-based logging — always active so background title_gen/hindsight paths are visible.
_LOG_DIR = Path.home() / ".juggle" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "juggle-cli.log"),
    ],
)

from juggle_settings import get_settings

NVIM_SOCKET = "/tmp/juggle-nvim.sock"


def _get_vault_root() -> Path:
    vault_val = get_settings()["paths"].get("vault", "/Documents/personal")
    if vault_val.startswith("~"):
        return Path(vault_val).expanduser()
    return Path.home() / vault_val.lstrip("/")


def _get_vault_name() -> str:
    explicit = get_settings()["paths"].get("vault_name", "")
    if explicit:
        return explicit
    return _get_vault_root().name


def cmd_vault_path(args):
    """Print the absolute vault root path (single source of truth for commands)."""
    print(str(_get_vault_root()))


def cmd_vault_name(args):
    """Print the vault name (used for obsidian:// URIs)."""
    print(_get_vault_name())


def cmd_verify(args):
    """Run the FULL test suite ONCE, synchronously.

    Agent-facing helper (2026-06-20): coder agents zombie-looped re-running the
    suite as a background job and polling it forever. `juggle verify` is the
    single deterministic command they call instead of hand-rolling pytest — it
    execs the WHOLE suite in the foreground exactly once (no subset, no
    --deselect: the full-suite directive). Extra args after `verify` pass through
    to pytest. Exit code is pytest's, so `juggle verify && ...` fails loudly.
    """
    # Base is the bare full-suite command (space-free tokens); passthrough args
    # (which CAN contain spaces, e.g. -k "a or b") are appended as discrete list
    # items so they are never re-split.
    cmd = ["uv", "run", "pytest", "-q"]
    cmd += list(args.pytest_args or [])
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


VAULT_ROOT = _get_vault_root()

# Re-export commonly used symbols for backward compatibility with tests
from juggle_cli_common import (  # noqa: F401
    _extract_decision_prompt,
    _last_sentences,
    get_db,
)
from juggle_cmd_misc import (  # noqa: F401 — re-export (handlers moved 2026-06-10)
    _cmd_list_selfheal,
    _cmd_selfheal_reset_diagnosing,
    _cmd_selfheal_set_status,
    _cmd_show_selfheal,
    _deny_matches,
    cmd_agent_tools,
    cmd_cockpit,
)

import juggle_cmd_autopilot
from juggle_cli_spec import build_parser
from juggle_cmd_graph import register_graph_parsers
from juggle_cmd_runs import register_runs_parsers
from juggle_cli_parsers_project import register_project_parsers
from juggle_cli_aliases import cmd_aliases
from juggle_cli_aliases import rewrite_argv as _rewrite_legacy_argv


def _obsidian_fallback(abs_file: str) -> None:
    """Open via Obsidian (vault files) or macOS system open (non-vault files)."""
    try:
        rel = Path(abs_file).relative_to(VAULT_ROOT)
        url = f"obsidian://open?vault={_get_vault_name()}&file={rel}"
        subprocess.run(["open", url], check=True)
    except ValueError:
        # File is outside the vault — Obsidian can't open it.
        print(f"nvim socket unavailable; opening with system default: {abs_file}")
        subprocess.run(["open", abs_file], check=True)


def _parse_path_with_line(spec: str) -> tuple[str, int | None]:
    """Split 'path:line' or 'path:line:col' into (path, line). Returns (spec, None) if no line."""
    m = re.match(r"^(.*?):(\d+)(?::\d+)?$", spec)
    if m:
        return m.group(1), int(m.group(2))
    return spec, None


def cmd_open_in_editor(args):
    path, line = _parse_path_with_line(args.file)
    abs_file = os.path.abspath(path)
    if os.path.exists(NVIM_SOCKET):
        try:
            subprocess.run(
                ["nvim", "--server", NVIM_SOCKET, "--remote", abs_file], check=True
            )
            if line is not None:
                subprocess.run(
                    [
                        "nvim",
                        "--server",
                        NVIM_SOCKET,
                        "--remote-send",
                        f"<C-\\><C-N>:{line}<CR>",
                    ],
                    check=True,
                )
            return
        except subprocess.CalledProcessError:
            pass
    _obsidian_fallback(abs_file)


def _subparsers_of(parser):
    """Return the root _SubParsersAction created by build_parser()."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise RuntimeError("build_parser produced no subparsers")


def _group_subparsers(subparsers, resource):
    """The _SubParsersAction under an existing ``resource`` group, or None."""
    group = subparsers.choices.get(resource)
    if group is None:
        return None
    for action in group._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def build_cli_parser(vault_path_default: str | None = None):
    """Build the full juggle CLI parser.

    The 51 flat commands come from the declarative COMMANDS table via
    build_parser() (P9 R4 — replaces the four hand-written register() walls). The
    already-grouped/conformant families (project/graph/project-graph/runs/
    autopilot) and the entry-module verbs (open-in-editor/vault-path/vault-name/
    verify) keep imperative registration and are layered on here.
    """
    if vault_path_default is None:
        vault_path_default = str(_get_vault_root())

    parser = build_parser()  # 51 flat COMMANDS + subparsers(dest="command")
    parser.description = "Juggle CLI - multi-topic conversation orchestrator"
    subparsers = _subparsers_of(parser)

    # grep-vault is now `vault grep` (resource group); its --vault-path default is
    # None in the static COMMANDS table. The entry point owns the runtime vault-root
    # default (cmd_grep_vault hands it straight to grep), so re-inject it here.
    vault_group = _group_subparsers(subparsers, "vault")
    grep_leaf = vault_group.choices.get("grep") if vault_group else None
    if grep_leaf is not None:
        for action in grep_leaf._actions:
            if action.dest == "vault_path":
                action.default = vault_path_default

    # Out-of-scope groups (already noun-verb; not ported into COMMANDS).
    register_graph_parsers(subparsers)
    register_runs_parsers(subparsers)
    register_project_parsers(subparsers)
    juggle_cmd_autopilot.register(subparsers)

    # vault path / vault name — entry-module verbs folded into the `vault` group
    # (G1; legacy vault-path/vault-name resolve via the alias shim). The `vault`
    # group already exists from `vault grep`.
    if vault_group is not None:
        p_vault_path = vault_group.add_parser("path", help="Print absolute vault root path")
        p_vault_path.set_defaults(func=cmd_vault_path)
        p_vault_name = vault_group.add_parser("name", help="Print vault name (for obsidian:// URIs)")
        p_vault_name.set_defaults(func=cmd_vault_name)

    # file open — open-in-editor folded into a `file` group (alias: open-in-editor)
    p_file = subparsers.add_parser("file", help="File operations")
    _file_sub = p_file.add_subparsers(dest="file_command", required=True)
    p_open = _file_sub.add_parser("open", help="Open file in nvim server")
    p_open.add_argument("file", help="Path to file to open")
    p_open.set_defaults(func=cmd_open_in_editor)

    # verify — run the FULL suite ONCE (agent helper). Extra args (incl.
    # leading-flag forms like `-k foo`) flow through via parse_known_args in
    # main(), so no REMAINDER positional (which mishandles a leading flag) here.
    p_verify = subparsers.add_parser(
        "verify",
        help="Run the FULL test suite once "
        "(extra args pass through to pytest)",
    )
    p_verify.set_defaults(func=cmd_verify, pytest_args=[])

    # aliases — dump the legacy→canonical alias map (A2; agent-verifiable §5 gate)
    p_aliases = subparsers.add_parser(
        "aliases", help="Show the legacy→canonical command alias map"
    )
    p_aliases.add_argument("--json", dest="json_out", action="store_true",
                           help="Emit the full map as a JSON object")
    p_aliases.set_defaults(func=cmd_aliases)
    return parser


def main():
    parser = build_cli_parser()

    # parse_known_args so `juggle verify` can pass arbitrary trailing args
    # (incl. leading-flag forms) through to pytest. For every OTHER command,
    # unknown args are still a hard error (strict re-parse preserves typo
    # rejection) — the leniency is scoped to verify only.
    rewritten = _rewrite_legacy_argv(sys.argv)[1:]
    args, _extras = parser.parse_known_args(rewritten)
    if _extras:
        if getattr(args, "command", None) == "verify":
            args.pytest_args = list(_extras)
        else:
            parser.parse_args(rewritten)  # re-raise SystemExit(2) with the usage message

    # Warn when watchdog is not running — it owns periodic reaping
    if "_JUGGLE_TEST_DB" not in os.environ:
        try:
            from juggle_watchdog_health import is_watchdog_alive
            if not is_watchdog_alive():
                print(
                    "Warning: juggle watchdog is not running or unresponsive. "
                    "Start it with: juggle start",
                    file=sys.stderr,
                )
        except Exception:
            pass

    try:
        args.func(args)
    except Exception as e:
        from juggle_selfheal import record_error
        record_error(e, "juggle_cli.main", {"argv": sys.argv})
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
