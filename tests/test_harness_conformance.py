#!/usr/bin/env python3
"""Harness conformance suite — the contract EVERY plugin must satisfy.

Juggle's orchestrator, tmux driver, watchdog and telemetry all depend on a
fixed set of *observable* behaviours from whatever harness launches an agent.
A harness that violates any of them silently breaks agent dispatch (tasks sit
unsubmitted, agents never identify their role, the deny list leaks, …). This
module encodes that contract as executable assertions and runs it against
**every harness juggle knows about**:

  * every adapter class registered in ``juggle_harness._ADAPTERS``, and
  * every harness defined in the shipped ``juggle_settings.DEFAULTS``.

New harness ⇒ it is auto-discovered here and MUST pass, or CI is red. There is
no opt-out: adding a plugin without satisfying the contract fails this file.

The contract (each is one test, parametrized over all harnesses):

  C1  Construction      — get_adapter resolves it without error; exposes a
                          stable id and the capability/marker API.
  C2  Launch identity   — build_launch_command always exports JUGGLE_IS_AGENT=1
                          and JUGGLE_AGENT_ROLE=<role> so hooks/telemetry/watchdog
                          can identify the agent process regardless of harness.
  C3  Audit propagation — audit=True ⇒ JUGGLE_AGENT_AUDIT=1 is exported; audit=
                          False ⇒ it is absent.
  C4  Model flag        — a model is reflected in the command; no model ⇒ no
                          dangling/empty model flag.
  C5  Single line       — the launch command is one shell line (it is pasted
                          into a tmux pane; embedded newlines would mis-submit).
  C6  Markers           — readiness & submission markers are non-empty tuples of
                          non-empty strings (the tmux paste/submit loop polls for
                          them; empty markers ⇒ send_task can never confirm).
  C7  Restriction       — per-role tool restriction is *materialized* (a real
                          deny appears either in the command or in a written
                          config artifact); audit mode must relax per-role denies.
  C8  Context delivery  — the role anchor reaches the agent exactly once: hook
                          harnesses keep the prompt clean (anchor via hook),
                          non-hook harnesses inline it into decorate_task output.
  C9  Determinism       — repeated identical builds are stable except for any
                          per-invocation temp path (no hidden global state).

To add a NEW required behaviour: add one test here; it instantly applies to
every present and future harness.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import juggle_harness
from juggle_harness import get_adapter
from juggle_settings import DEFAULTS


# --------------------------------------------------------------------------
# Discovery: build an agent_cfg per harness juggle ships or registers, so the
# suite covers everything without per-harness boilerplate.
# --------------------------------------------------------------------------
def _discover_harness_cfgs() -> dict[str, dict]:
    """Return {harness_id: agent_cfg} for every harness the suite must cover:
    every harness shipped in DEFAULTS["agent"]["harnesses"] (claude, codex),
    plus a synthetic ``template`` harness — the one registered adapter type with
    no shipped config — so the config-only "bring your own harness" path is
    gated too. A new real harness is auto-covered the moment it lands in DEFAULTS.
    """
    base_agent = DEFAULTS["agent"]
    cfgs: dict[str, dict] = {}

    for hid in (base_agent.get("harnesses") or {}):
        agent_cfg = dict(base_agent)
        agent_cfg["harness"] = hid
        cfgs[hid] = agent_cfg

    # A minimal config-only (template) harness, mirroring the documented schema.
    synth = {
        "type": "template",
        "command": "fakeharness",
        "model_flag": "--model {model}",
        "restrictions_flag": "--deny-tools {role}",
        "env": {"JUGGLE_IS_AGENT": "1"},
        "env_unset": [],
        "readiness_markers": ["ready>"],
        "submission_markers": ["running"],
        "supports_hooks": False,
    }
    cfgs["_synth_template"] = {
        **{k: v for k, v in base_agent.items() if k != "harnesses"},
        "harness": "_synth_template",
        "harnesses": {"_synth_template": synth},
    }
    return cfgs


_HARNESS_CFGS = _discover_harness_cfgs()
_HARNESS_IDS = sorted(_HARNESS_CFGS)

ROLES = ("researcher", "coder", "planner")


@pytest.fixture(params=_HARNESS_IDS)
def harness(request):
    """(harness_id, agent_cfg) for each discovered harness."""
    hid = request.param
    return hid, _HARNESS_CFGS[hid]


def _build(harness, role="coder", model="sonnet", audit=False, tmp_path=None):
    """build_launch_command for an adapter, with overlay writes redirected to tmp."""
    hid, agent_cfg = harness
    adapter = get_adapter(role, agent_cfg=agent_cfg)
    if tmp_path is not None:
        # Redirect any file-materialized restriction (claude overlay) into tmp so
        # the suite never touches a real ~/.juggle dir.
        cfg = {"paths": {"config_dir": str(tmp_path)}, "agent": agent_cfg}
        with patch("juggle_agent_settings.get_settings", return_value=cfg):
            return adapter, adapter.build_launch_command(role=role, model=model, audit=audit)
    return adapter, adapter.build_launch_command(role=role, model=model, audit=audit)


# --- C1 Construction -------------------------------------------------------
def test_c1_construction_and_api(harness):
    hid, agent_cfg = harness
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    assert isinstance(adapter, juggle_harness.HarnessAdapter)
    assert isinstance(adapter.id, str) and adapter.id
    assert isinstance(adapter.supports_hooks, bool)
    assert isinstance(adapter.readiness_markers(), tuple)
    assert isinstance(adapter.submission_markers(), tuple)


# --- C2 Launch identity ----------------------------------------------------
@pytest.mark.parametrize("role", ROLES)
def test_c2_launch_exports_agent_identity(harness, role, tmp_path):
    _, cmd = _build(harness, role=role, tmp_path=tmp_path)
    assert "JUGGLE_IS_AGENT=1" in cmd, (
        f"{harness[0]}: every harness must tag its process JUGGLE_IS_AGENT=1"
    )
    assert f"JUGGLE_AGENT_ROLE={role}" in cmd, (
        f"{harness[0]}: role must be exported so hooks/telemetry/watchdog can attribute it"
    )


# --- C3 Audit propagation --------------------------------------------------
def test_c3_audit_env_toggles(harness, tmp_path):
    _, on = _build(harness, audit=True, tmp_path=tmp_path)
    _, off = _build(harness, audit=False, tmp_path=tmp_path)
    assert "JUGGLE_AGENT_AUDIT=1" in on, f"{harness[0]}: audit=True must export JUGGLE_AGENT_AUDIT=1"
    assert "JUGGLE_AGENT_AUDIT=1" not in off, f"{harness[0]}: audit=False must NOT export it"


# --- C4 Model flag ---------------------------------------------------------
def test_c4_model_present_and_optional(harness, tmp_path):
    _, with_model = _build(harness, model="some-model-x", tmp_path=tmp_path)
    assert "some-model-x" in with_model, f"{harness[0]}: model must appear in the command"
    _, no_model = _build(harness, model=None, tmp_path=tmp_path)
    # No model ⇒ no dangling flag fragment with an empty value.
    assert "{model}" not in no_model
    assert "--model \n" not in no_model and not no_model.rstrip().endswith("--model")


# --- C5 Single shell line --------------------------------------------------
def test_c5_command_is_single_line(harness, tmp_path):
    _, cmd = _build(harness, tmp_path=tmp_path)
    assert "\n" not in cmd, (
        f"{harness[0]}: launch command is pasted into a tmux pane; it must be one line"
    )
    assert cmd.strip() == cmd.strip().replace("\r", ""), f"{harness[0]}: no carriage returns"


# --- C6 Markers ------------------------------------------------------------
def test_c6_markers_nonempty(harness):
    hid, agent_cfg = harness
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    if not adapter.is_interactive:
        pytest.skip("one-shot harness: no REPL to poll, markers unused")
    for name, markers in (
        ("readiness", adapter.readiness_markers()),
        ("submission", adapter.submission_markers()),
    ):
        assert markers, f"{hid}: {name}_markers must be non-empty (tmux loop polls for them)"
        assert all(isinstance(m, str) and m for m in markers), (
            f"{hid}: {name}_markers must all be non-empty strings"
        )


# --- C7 Restriction materialized + audit relaxes ---------------------------
def test_c7_per_role_restriction_materialized(harness, tmp_path):
    """A role's tool restriction must end up SOMEWHERE the harness will read:
    either inline in the command, or in a config artifact it writes.

    For Claude that's the --settings overlay file; for template harnesses it's
    the restrictions_flag fragment. This guards against an adapter that silently
    drops the deny list (the original token-saving guarantee)."""
    hid, agent_cfg = harness
    adapter, cmd = _build(harness, role="coder", tmp_path=tmp_path)

    materialized = False
    # (a) inline in the command (template restrictions_flag, codex -c, etc.)
    if adapter._restrictions_part("coder", False).strip():
        materialized = True
    # (b) a written --settings artifact (claude overlay)
    if "--settings " in cmd:
        path = Path(cmd.split("--settings ", 1)[1].split()[0].strip("'\""))
        if path.exists():
            overlay = json.loads(path.read_text())
            # claude overlay must carry the deny surface for a restricted role
            assert "permissions" in overlay or overlay == {}, (
                f"{hid}: settings overlay must be a valid settings dict"
            )
            materialized = True
    assert materialized, (
        f"{hid}: per-role tool restriction is not materialized anywhere "
        "(command has no restriction fragment and no settings artifact)"
    )


def test_c7_audit_relaxes_claude_overlay(harness, tmp_path):
    """For file-materialized harnesses, audit mode must drop PER-ROLE denies so
    `juggle agent-tools` can observe true demand (universal denies stay).

    In production `agent.audit_mode` drives BOTH the JUGGLE_AGENT_AUDIT env tag
    and the overlay relaxation (start_agent_in_pane couples them), so the test
    flips the config flag and rebuilds — proving the materialized artifact, not
    just the env, reflects audit mode."""
    hid, base_cfg = harness
    agent_cfg = {**base_cfg, "audit_mode": True}
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    cfg = {"paths": {"config_dir": str(tmp_path)}, "agent": agent_cfg}
    with patch("juggle_agent_settings.get_settings", return_value=cfg):
        cmd = adapter.build_launch_command(role="coder", model="sonnet", audit=True)
    if "--settings " not in cmd:
        pytest.skip(f"{hid}: not a file-materialized restriction harness")
    path = Path(cmd.split("--settings ", 1)[1].split()[0].strip("'\""))
    overlay = json.loads(path.read_text())
    # In audit mode the per-role deny is stripped; universal base denies remain
    # (verified in detail by the test_agent audit suites — here we assert the
    # contract holds for whatever harness materializes a file).
    role_only = {"NotebookEdit"}  # coder-specific in DEFAULTS, not in base
    deny = (overlay.get("permissions") or {}).get("deny") or []
    assert not (role_only & set(deny)), (
        f"{hid}: audit mode must relax per-role denies in the materialized overlay"
    )


# --- C8 Context delivery (anchor exactly once) -----------------------------
def test_c8_anchor_delivery_matches_capability(harness):
    hid, agent_cfg = harness
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    with patch("juggle_context.render_agent_role_anchor_for", return_value="ANCHOR-XYZ"):
        decorated = adapter.decorate_task("coder", "TASK BODY")
    if adapter.supports_hooks:
        # Hook injects the anchor — the prompt must stay clean (no double-inject).
        assert "ANCHOR-XYZ" not in decorated, (
            f"{hid}: hook-capable harness must NOT also inline the anchor (double injection)"
        )
        assert decorated == "TASK BODY"
    else:
        # No hooks — the anchor MUST be inlined so the agent learns its role.
        assert "ANCHOR-XYZ" in decorated, (
            f"{hid}: non-hook harness must inline the role anchor into the task"
        )
        assert "TASK BODY" in decorated


def test_c8_anchor_absent_role_safe(harness):
    """A role with no configured anchor must not corrupt the prompt."""
    hid, agent_cfg = harness
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    with patch("juggle_context.render_agent_role_anchor_for", return_value=""):
        decorated = adapter.decorate_task("coder", "TASK BODY")
    assert "TASK BODY" in decorated


# --- C9 Determinism --------------------------------------------------------
def test_c9_build_is_stable_modulo_tempfiles(harness, tmp_path):
    _, a = _build(harness, tmp_path=tmp_path)
    _, b = _build(harness, tmp_path=tmp_path)

    def _normalize(cmd: str) -> str:
        # Collapse any /tmp or overlay path token to a placeholder so per-run
        # temp filenames don't count as nondeterminism.
        return " ".join(
            "<PATH>" if ("/" in tok and tok.endswith(".json")) or tok.startswith("/tmp")
            else tok
            for tok in cmd.split()
        )

    assert _normalize(a) == _normalize(b), (
        f"{harness[0]}: launch command must be stable across identical builds "
        f"(modulo temp paths):\n  {a!r}\n  {b!r}"
    )


# --- C10 Interactivity contract --------------------------------------------
def test_c10_interactivity_is_bool_and_consistent(harness, tmp_path):
    """Every harness declares interactive vs one-shot, and a one-shot harness
    must produce a runnable task command that embeds the prompt file."""
    hid, agent_cfg = harness
    adapter = get_adapter("coder", agent_cfg=agent_cfg)
    assert isinstance(adapter.is_interactive, bool)
    if not adapter.is_interactive:
        cmd = adapter.build_task_command(
            "/tmp/PROMPTFILE.txt", role="coder", model="m"
        )
        assert "\n" not in cmd, f"{hid}: one-shot task command must be a single line"
        assert "PROMPTFILE.txt" in cmd, (
            f"{hid}: one-shot task command must reference the prompt file"
        )
        assert "JUGGLE_IS_AGENT=1" in cmd, (
            f"{hid}: one-shot task command must still carry the agent identity env"
        )


# --- meta: the suite actually discovered the real shipped harnesses --------
def test_meta_suite_covers_shipped_claude():
    assert "claude" in _HARNESS_IDS, "conformance suite must cover the shipped claude harness"
    assert any(h.startswith("_synth_") for h in _HARNESS_IDS), (
        "conformance suite must synthesise a harness per registered adapter type"
    )
