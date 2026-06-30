"""P9 G3-verb-lint: closed-verb-vocabulary lint (spec §2.2).

Every resource-scoped command's verb must be drawn from the closed action
vocabulary defined in §2.2 of the CLI-grammar-migration spec
(``docs/2026-06-29-cli-grammar-migration-spec.md`` in the vault). Per §2.2 the
set is CLOSED: "New commands MUST reuse a verb from this list; additions
require updating the lint allowlist." This lint is that enforcement gate — it
fails the build the moment a command introduces a verb outside the sanctioned
vocabulary, so the action grammar cannot silently sprawl.

Top-level flat commands (``Cmd.resource is None`` — ``start``/``stop``/
``doctor``/``cockpit``/``integrate``/``verify``) are §2.1 "kept flat" and are
NOT part of the noun-verb grammar, so they are exempt from the verb-vocabulary
check by construction.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cli_commands import COMMANDS  # noqa: E402
from juggle_cli_spec import Cmd  # noqa: E402


# ── §2.2 closed verb vocabulary (the canonical action set, verbatim) ──────────
CLOSED_VERBS = frozenset({
    "create", "list", "show", "get", "update", "delete", "close",
    "archive", "unarchive", "send", "complete", "fail", "ack", "notify",
    "spawn", "release", "decommission", "set", "reset", "propose", "audit",
    "restore", "prune", "reconcile", "mark", "run", "flush", "migrate",
    "retain", "grep", "next",
})

# ── §2.3 compound verbs (a closed verb + a disambiguating qualifier) ──────────
# §2.3 materializes several commands as "<closed-verb>-<qualifier>" so siblings
# that share an action stay distinct (e.g. agent has both send-task AND
# send-message → both reuse the closed verb ``send``). Each stem below is a
# member of CLOSED_VERBS, so these "reuse a verb from the list" per §2.2.
COMPOUND_VERBS = frozenset({
    "send-task",             # agent    — stem: send
    "send-message",          # agent    — stem: send
    "set-watchdog",          # agent    — stem: set
    "set-status",            # selfheal — stem: set
    "set-summarized-count",  # thread   — stem: set
    "archive-candidates",    # thread   — stem: archive
    "list-stale",            # thread   — stem: list
})

# ── §2.3 retained non-canonical verbs (documented divergence) ─────────────────
# §2.3's old→new mapping fixes these resource verbs verbatim even though they
# are NOT in §2.2's closed list. They are enumerated here (not silently waved
# through) so the lint stays green today yet still fails on any BRAND-NEW
# out-of-vocabulary verb. Renaming them to a closed verb is out of scope for the
# G3 lint and tracked by the later migration nodes.
RETAINED_VERBS = frozenset({
    "switch",    # thread switch   (switch-thread)
    "messages",  # thread messages (get-messages)
    "check",     # agent check     (check-agents; §2.3 may merge into list --json)
    "tools",     # agent tools     (agent-tools)
    "stop",      # watchdog stop   (stop-watchdog)
    "init",      # db init         (init-db — closed set has migrate/flush, not init)
    "digest",    # context digest  (digest)
    "dogfood",   # schedule dogfood
    "autofix",   # schedule autofix
    "reflect",   # schedule reflect
})

ALLOWED_VERBS = CLOSED_VERBS | COMPOUND_VERBS | RETAINED_VERBS


def _resource_verbs():
    """(resource, verb) for every resource-scoped command (flat verbs exempt)."""
    return [(c.resource, c.verb) for c in COMMANDS if c.resource is not None]


def test_every_resource_verb_in_closed_vocab():
    """REGRESSION PIN (P9 G3, spec §2.2): every resource-scoped COMMANDS verb is
    in the closed verb allowlist; a verb outside it fails the build."""
    offenders = [(r, v) for r, v in _resource_verbs() if v not in ALLOWED_VERBS]
    assert not offenders, (
        "verbs outside the §2.2 closed vocabulary "
        f"(add to the closed set or the documented extension): {sorted(offenders)}"
    )


def test_lint_has_teeth_rejects_novel_verb():
    """The lint must be non-vacuous: a synthetic command with a made-up verb is
    rejected by the same allowlist check the production lint uses."""
    bogus = Cmd("thread", "frobnicate", handler=lambda a: None)
    assert bogus.verb not in ALLOWED_VERBS
