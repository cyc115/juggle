"""juggle_cli_spec — declarative CLI command spec dataclasses (P9 R1).

Single source of truth for the juggle CLI surface: the ``COMMANDS`` tuple of ``Cmd``
entries replaces the four hand-written ``add_parser`` walls. This module defines the
``Cmd``/``Arg`` dataclasses, ``Arg.add_to`` (the declarative→argparse translator),
the ``COMMANDS`` table, and the generic ``build_parser`` registrar. ``build_parser``
is PARALLEL to the four hand-written ``register()`` walls and is NOT wired into
``main()`` yet (R3 populates ``COMMANDS`` from the real handlers; R4 switches the
entrypoint over). No handler imports live here, so importing this module has zero
side effects and does not change any existing CLI behavior.

Spec: docs CLI-grammar-migration §3 (spec-table sketch).

    Cmd("thread", "create", cmd_create_thread,
        args=(Arg("topic"),), aliases=("create-thread",), help="Create a topic thread")
    Cmd(None, "verify", cmd_verify, passthrough=True)   # top-level global verb
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Callable

# Sentinel distinguishing "caller did not set this field" from a legitimate
# ``None``/falsy argparse value (e.g. ``default=None``). Hashable, so frozen
# Cmd/Arg instances stay hashable.
_UNSET: Any = object()


@dataclass(frozen=True)
class Arg:
    """One argparse argument, declared as data.

    ``name`` is a positional (``"topic"``) or an optional flag (``"--retain"``).
    Every other field maps 1:1 to an ``argparse.add_argument`` keyword and is only
    forwarded when explicitly set (``_UNSET`` fields are omitted, so argparse's own
    defaults apply). ``add_to`` performs the translation; no parser is built here.
    """

    name: str
    dest: str | None = None
    help: str = ""
    action: Any = _UNSET
    nargs: Any = _UNSET
    type: Any = _UNSET
    choices: Any = _UNSET
    default: Any = _UNSET
    const: Any = _UNSET
    required: Any = _UNSET
    metavar: Any = _UNSET

    @property
    def is_positional(self) -> bool:
        return not self.name.startswith("-")

    def add_to(self, parser) -> None:
        """Apply this argument to ``parser`` via ``add_argument``."""
        kwargs: dict[str, Any] = {}
        if self.help:
            kwargs["help"] = self.help
        # ``dest`` is only valid for optionals; positionals derive it from name.
        if self.dest is not None and not self.is_positional:
            kwargs["dest"] = self.dest
        for key in (
            "action", "nargs", "type", "choices", "default", "const",
            "required", "metavar",
        ):
            value = getattr(self, key)
            if value is not _UNSET:
                kwargs[key] = value
        parser.add_argument(self.name, **kwargs)


@dataclass(frozen=True)
class Cmd:
    """One CLI command in the uniform ``juggle <resource> <verb>`` grammar.

    ``resource is None`` marks a top-level global verb (e.g. ``start``, ``verify``,
    ``doctor``) that reads better flat. ``aliases`` holds legacy flat names the
    backward-compat shim (A1) will rewrite to ``[resource, verb]``. ``passthrough``
    flags a command parsed with ``parse_known_args`` (only ``verify`` today).
    """

    resource: str | None
    verb: str
    handler: Callable[[Any], Any]
    args: tuple[Arg, ...] = ()
    aliases: tuple[str, ...] = ()
    help: str = ""
    passthrough: bool = False


# The declarative command table. Empty scaffold here; R3 ports the four
# register() walls into real Cmd entries (importing the handlers).
COMMANDS: tuple[Cmd, ...] = ()


def build_parser(
    commands: Iterable[Cmd] = COMMANDS, *, prog: str = "juggle"
) -> argparse.ArgumentParser:
    """Build an argparse parser from declarative ``Cmd`` entries (§3).

    Top-level global verbs (``resource is None``) attach directly under the root
    subparsers; resource-scoped commands group under a per-resource subparsers
    object (``juggle <resource> <verb>``), created once per resource and reused.
    Each leaf gets its declared ``Arg``\\s and ``set_defaults(func=handler)``.

    Legacy ``aliases`` are intentionally NOT registered here — the backward-compat
    layer (A1) rewrites legacy argv to ``[resource, verb]`` BEFORE this parser runs,
    so the parser tree stays canonical-only. ``passthrough`` likewise is consumed at
    dispatch time (``parse_known_args``), not expressed in the tree.

    PARALLEL + UNUSED: nothing calls this yet; ``main()`` still uses the hand-written
    walls until R4.
    """
    parser = argparse.ArgumentParser(prog=prog)
    sub = parser.add_subparsers(dest="command", required=True)
    groups: dict[str, Any] = {}  # resource -> its add_subparsers() object
    for c in commands:
        if c.resource is None:
            leaf = sub.add_parser(c.verb, help=c.help)
        else:
            group = groups.get(c.resource)
            if group is None:
                resource_parser = sub.add_parser(
                    c.resource, help=f"{c.resource} commands"
                )
                group = resource_parser.add_subparsers(
                    dest=f"{c.resource}_command", required=True
                )
                groups[c.resource] = group
            leaf = group.add_parser(c.verb, help=c.help)
        for arg in c.args:
            arg.add_to(leaf)
        leaf.set_defaults(func=c.handler)
    return parser
