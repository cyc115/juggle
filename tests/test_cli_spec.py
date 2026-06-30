"""P9 R1-spec-scaffold: the declarative CLI command spec dataclasses.

Pins the pure-data layer (Cmd/Arg) that later nodes (R2 build_parser, A1 alias
shim) consume. No CLI wiring exists yet — these tests only exercise the
dataclasses and Arg.add_to()'s translation to argparse.add_argument.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli_spec import Arg, Cmd  # noqa: E402


# ── Arg.add_to → argparse ─────────────────────────────────────────────────────


def test_arg_positional_round_trips_through_argparse():
    p = argparse.ArgumentParser()
    Arg("topic", help="Topic name").add_to(p)
    assert p.parse_args(["hello"]).topic == "hello"


def test_arg_flag_with_dest():
    p = argparse.ArgumentParser()
    Arg("--retain", dest="retain_text").add_to(p)
    assert p.parse_args(["--retain", "y"]).retain_text == "y"


def test_arg_store_true_action():
    p = argparse.ArgumentParser()
    Arg("--json", dest="json_out", action="store_true").add_to(p)
    assert p.parse_args([]).json_out is False
    assert p.parse_args(["--json"]).json_out is True


def test_arg_default_and_type_and_choices():
    p = argparse.ArgumentParser()
    Arg("--threshold", dest="threshold", type=int, default=3).add_to(p)
    Arg("--priority", choices=("low", "high"), default="low").add_to(p)
    ns = p.parse_args([])
    assert ns.threshold == 3 and ns.priority == "low"
    assert p.parse_args(["--threshold", "9", "--priority", "high"]).threshold == 9


def test_arg_nargs_positional():
    p = argparse.ArgumentParser()
    Arg("terms", nargs="+").add_to(p)
    assert p.parse_args(["a", "b"]).terms == ["a", "b"]


# ── Cmd dataclass shape ───────────────────────────────────────────────────────


def _noop(args):  # a real handler reference (not a lambda) for clarity
    return None


def test_cmd_fields_and_defaults():
    c = Cmd("thread", "create", _noop, args=(Arg("topic"),), aliases=("create-thread",),
            help="Create a topic thread")
    assert c.resource == "thread"
    assert c.verb == "create"
    assert c.handler is _noop
    assert c.args == (Arg("topic"),)
    assert c.aliases == ("create-thread",)
    assert c.passthrough is False  # default


def test_cmd_top_level_global_verb_has_none_resource():
    c = Cmd(None, "verify", _noop, passthrough=True)
    assert c.resource is None
    assert c.passthrough is True


def test_cmd_and_arg_are_frozen():
    c = Cmd("agent", "list", _noop)
    a = Arg("--json", action="store_true")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.verb = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.name = "y"  # type: ignore[misc]


def test_cmd_is_hashable_pure_data():
    c = Cmd("thread", "create", _noop, args=(Arg("topic"),), aliases=("create-thread",))
    assert isinstance(hash(c), int)  # frozen + hashable → usable in sets/dicts
