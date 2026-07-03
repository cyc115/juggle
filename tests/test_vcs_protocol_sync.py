"""Protocol/conformance-kit/guide sync pins (SPEC 2026-07-02 §7.7/§7.8/§7.9).

Three artifacts must stay in lockstep with the ``VCS`` Protocol: the
``VCS_PROTOCOL_VERSION`` constant, the conformance kit (this file's first two
tests), and the agent-facing backend guide (extended by the vcs-backend-guide
topic — the hook for that third assertion lives at the bottom of this file).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import vcs as vcs_mod  # noqa: E402

# Frozen snapshot of every VCS Protocol member for VCS_PROTOCOL_VERSION == 1.
# Adding/removing/renaming a method MUST update this set AND bump
# VCS_PROTOCOL_VERSION in the same change — that pairing is what this test
# enforces (§7.7): a version bump with no set change, or a set change with no
# version bump, both fail here.
PROTOCOL_V1_MEMBERS = frozenset({
    "name", "capabilities",
    "repo_root", "primary_root",
    "refresh", "trunk", "resolve", "is_ancestor",
    "create_workspace", "remove_workspace", "current_rev", "dirty_files",
    "has_changes", "update_to", "describe_changes",
    "submit", "land_status",
    "head", "is_dirty", "make_safety_branch",
})


def _protocol_members() -> set[str]:
    methods = {m for m in dir(vcs_mod.VCS) if not m.startswith("_")}
    annotated = set(getattr(vcs_mod.VCS, "__annotations__", {}))
    return methods | annotated


def test_protocol_method_set_matches_frozen_snapshot_for_current_version():
    """If this fails, either the Protocol changed without bumping
    VCS_PROTOCOL_VERSION, or PROTOCOL_V1_MEMBERS is stale — update BOTH
    together, never one alone."""
    assert vcs_mod.VCS_PROTOCOL_VERSION == 1, (
        "VCS_PROTOCOL_VERSION advanced — PROTOCOL_V1_MEMBERS (and its name) "
        "must be updated to match the new version's frozen method set"
    )
    assert _protocol_members() == PROTOCOL_V1_MEMBERS, (
        "VCS Protocol member set drifted from the PROTOCOL_V1_MEMBERS snapshot "
        "— bump VCS_PROTOCOL_VERSION and update this snapshot together"
    )


def test_every_protocol_method_has_a_conformance_test():
    """Every §1.1 Protocol METHOD (excluding plain data attrs name/capabilities
    and the back-compat aliases, which conformance covers transitively via the
    seam methods that back them) appears as a literal ``h.vcs.<method>`` call
    somewhere in vcs_conformance.py — widening the Protocol without extending
    the kit fails here (§7.8)."""
    seam_methods = PROTOCOL_V1_MEMBERS - {
        "name", "capabilities", "head", "is_dirty", "make_safety_branch",
    }
    conformance_src = (REPO_ROOT / "src" / "vcs_conformance.py").read_text()
    missing = [
        m for m in sorted(seam_methods)
        if f"h.vcs.{m}(" not in conformance_src
    ]
    assert not missing, f"Protocol methods with no conformance coverage: {missing}"


# ── Guide-sync hook (extended by the vcs-backend-guide topic) ───────────────
# def test_every_protocol_method_named_in_the_backend_guide(): ...
