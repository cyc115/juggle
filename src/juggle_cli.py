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
    """Run the test suite ONCE, synchronously, with the quarantined reds deselected.

    Agent-facing helper (2026-06-20): coder agents zombie-looped re-running the
    FULL suite on pre-existing quarantined tests. `juggle verify` is the single
    deterministic command they call instead of hand-rolling pytest — it reads
    integrate.quarantine_tests and prepends the --deselect flags, then execs
    pytest in the foreground exactly once. Extra args after `verify` pass through
    to pytest. Exit code is pytest's, so `juggle verify && ...` fails loudly.
    """
    from juggle_integrate_testscope import apply_quarantine

    quarantine = get_settings()["integrate"].get("quarantine_tests", [])
    # Build the deselect-bearing base, then append passthrough args as-is so an
    # arg containing spaces (e.g. -k "a or b") is never re-split into tokens.
    # Invariant: the base + quarantine paths are space-free (pytest node ids),
    # so the .split() round-trip is safe; passthrough args (which CAN contain
    # spaces) are added as discrete list items below, never via split.
    cmd = apply_quarantine("uv run pytest -q", quarantine).split()
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
    _deny_matches,
    cmd_agent_tools,
    cmd_cockpit,
)

import juggle_cli_parsers_agents
import juggle_cli_parsers_misc
import juggle_cli_parsers_threads
import juggle_cmd_autopilot


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


def main():
    parser = argparse.ArgumentParser(
        description="Juggle CLI - multi-topic conversation orchestrator"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    juggle_cli_parsers_threads.register(subparsers)
    juggle_cli_parsers_agents.register(subparsers)
    juggle_cli_parsers_misc.register(
        subparsers, vault_path_default=str(_get_vault_root())
    )
    juggle_cmd_autopilot.register(subparsers)

    # open-in-editor (handler stays in this module — patch surface for tests)
    p_open = subparsers.add_parser("open-in-editor", help="Open file in nvim server")
    p_open.add_argument("file", help="Path to file to open")
    p_open.set_defaults(func=cmd_open_in_editor)

    # vault-path / vault-name (single source of truth for commands resolving the vault)
    p_vault_path = subparsers.add_parser("vault-path", help="Print absolute vault root path")
    p_vault_path.set_defaults(func=cmd_vault_path)
    p_vault_name = subparsers.add_parser("vault-name", help="Print vault name (for obsidian:// URIs)")
    p_vault_name.set_defaults(func=cmd_vault_name)

    # verify — run the suite ONCE with quarantined reds deselected (agent helper).
    # Extra args (incl. leading-flag forms like `-k foo`) flow through via
    # parse_known_args below, so no REMAINDER positional (which mishandles a
    # leading flag) is declared here.
    p_verify = subparsers.add_parser(
        "verify",
        help="Run the test suite once with quarantined reds deselected "
        "(extra args pass through to pytest)",
    )
    p_verify.set_defaults(func=cmd_verify, pytest_args=[])

    # parse_known_args so `juggle verify` can pass arbitrary trailing args
    # (incl. leading-flag forms) through to pytest. For every OTHER command,
    # unknown args are still a hard error (strict re-parse preserves typo
    # rejection) — the leniency is scoped to verify only.
    args, _extras = parser.parse_known_args()
    if _extras:
        if getattr(args, "command", None) == "verify":
            args.pytest_args = list(_extras)
        else:
            parser.parse_args()  # re-raise SystemExit(2) with the usage message

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
