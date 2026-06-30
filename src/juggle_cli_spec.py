"""juggle_cli_spec — declarative CLI command spec dataclasses (P9 R1).

Single source of truth for the juggle CLI surface: a future ``COMMANDS`` tuple of
``Cmd`` entries replaces the four hand-written ``add_parser`` walls. This module is
PURE DATA — it defines the ``Cmd``/``Arg`` dataclasses and ``Arg.add_to`` (the
declarative→argparse translator) only. No ``COMMANDS`` table, no ``build_parser``,
no handler imports, no CLI wiring yet (those land in R2+). Importing this module
has zero side effects and does not change any existing CLI behavior.

Spec: docs CLI-grammar-migration §3 (spec-table sketch).

    Cmd("thread", "create", cmd_create_thread,
        args=(Arg("topic"),), aliases=("create-thread",), help="Create a topic thread")
    Cmd(None, "verify", cmd_verify, passthrough=True)   # top-level global verb
"""

from __future__ import annotations

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
