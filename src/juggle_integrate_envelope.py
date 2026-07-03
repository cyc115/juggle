"""Juggle — integrate fail envelope (irl-envelope R2/T2, plan
2026-07-03-integrate-recovery-loop.md, spec rev 6 §2).

R2: extracts `_fail`'s action-item bookkeeping out of
`juggle_cmd_integrate._run_integrate` into `record_refusal()` — zero behavior
change, refusal message text pinned unchanged. `_fail` keeps control flow
only (lock release + early return).

T2: adds `classify()` — mechanical, from the failing STEP, into one of 5
classes (conflict/red-suite/divergence/collision/machinery) — envelope
assembly, and `_fail` emitting the `integrate_failed`/`machinery_error`
event. `machinery` is ALWAYS routed to the orchestrator (never auto-repaired
— a pipeline bug is not the topic's fault); every other class emits
`integrate_failed` with `handled_by='watchdog'` for now (T3 will override
this once attempt-cap/novelty-gate counters exist).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

CONFLICT = "conflict"
RED_SUITE = "red-suite"
DIVERGENCE = "divergence"
COLLISION = "collision"
MACHINERY = "machinery"

ALL_CLASSES = frozenset({CONFLICT, RED_SUITE, DIVERGENCE, COLLISION, MACHINERY})

# `_fail` call-site identifiers — classification is mechanical from these,
# never from message text (spec §2).
STEP_DIRTY_WORKTREE = "dirty_worktree"
STEP_NO_MAIN_BRANCH = "no_main_branch"
STEP_EMPTY_BRANCH = "empty_branch"
STEP_REBASE_CONFLICT = "rebase_conflict"
STEP_TEST_FAILURE = "test_failure"
STEP_SUBMIT_FAILED = "submit_failed"
STEP_UNEXPECTED = "unexpected_exception"

_STEP_TO_CLASS = {
    STEP_DIRTY_WORKTREE: COLLISION,
    STEP_NO_MAIN_BRANCH: MACHINERY,
    STEP_EMPTY_BRANCH: MACHINERY,
    STEP_REBASE_CONFLICT: CONFLICT,
    STEP_TEST_FAILURE: RED_SUITE,
    STEP_SUBMIT_FAILED: DIVERGENCE,
    STEP_UNEXPECTED: MACHINERY,
}


def resolve_attempt_shas(
    backend, main_repo_path: str, worktree_path: str, rebase_onto: str | None,
) -> tuple[str | None, str | None]:
    """Best-effort trunk/HEAD SHAs at failure time — never a gate."""
    trunk_sha = head_sha = None
    try:
        if rebase_onto:
            trunk_sha = backend.resolve(main_repo_path, rebase_onto)
        head_sha = backend.resolve(worktree_path, "HEAD")
    except Exception:
        pass
    return trunk_sha, head_sha


def classify(step: str, detail: str = "") -> str:
    """Classify a `_fail` ``step`` into one of ALL_CLASSES.

    Mechanical from the failing step (spec §2) — ``detail`` (the prose
    reason) is never inspected for classification, only carried onward into
    the envelope; unrecognized steps default to machinery (fail-closed: an
    unclassified pipeline path is treated as a pipeline defect, not silently
    handed to a repair playbook that doesn't fit it).
    """
    return _STEP_TO_CLASS.get(step, MACHINERY)


@dataclass
class FailContext:
    """Everything `record_refusal` needs beyond (step, detail) — bundled so
    the `_fail` closure in juggle_cmd_integrate stays a thin control-flow
    wrapper (R2)."""

    db: Any
    thread_id: str
    worktree_branch: str
    worktree_path: str = ""
    task: dict | None = None
    session_id: str = ""
    files: list[str] = field(default_factory=list)
    log_tail: str = ""
    trunk_at_attempt: str | None = None
    head_at_attempt: str | None = None


@dataclass
class FailRecord:
    step: str
    reason: str
    message: str
    fail_class: str
    envelope: dict


def build_envelope(fail_class: str, ctx: FailContext) -> dict:
    """Assemble the fail envelope dict (spec §2 shape)."""
    return {
        "class": fail_class,
        "files": ctx.files,
        "log_tail": ctx.log_tail,
        "branch": ctx.worktree_branch,
        "worktree": ctx.worktree_path,
        "attempt": int((ctx.task or {}).get("verify_retries") or 0),
        "trunk_at_attempt": ctx.trunk_at_attempt,
        "head_at_attempt": ctx.head_at_attempt,
    }


def handled_by_for(fail_class: str) -> tuple[str, str]:
    """Return ``(event_kind, handled_by)`` for a fail_class.

    machinery is ALWAYS ``machinery_error`` / orchestrator (spec §3 — never
    auto-repaired). Every other class is ``integrate_failed``; handled_by
    defaults to watchdog (routine repair dispatch, T4) until T3's
    attempt-cap/novelty-gate logic overrides it at emit time.
    """
    if fail_class == MACHINERY:
        return "machinery_error", "orchestrator"
    return "integrate_failed", "watchdog"


def record_refusal(step: str, detail: str, ctx: FailContext) -> FailRecord:
    """R2+T2: the DB bookkeeping half of a refused integrate.

    Writes the pinned prose action item (message text UNCHANGED from before
    the extraction), then (T2) persists the fail envelope onto the bound
    graph task and emits the routed event. Control flow (lock release, early
    return) stays in `_fail`.
    """
    message = f"⚠️ integrate failed [{ctx.worktree_branch}]: {detail}"
    ctx.db.add_action_item(
        thread_id=ctx.thread_id, message=message, type_="manual_step", priority="high",
    )

    fail_class = classify(step, detail)
    envelope = build_envelope(fail_class, ctx)
    if ctx.task:
        from dbops import db_graph

        db_graph.set_task_fail_envelope(ctx.db, ctx.task["id"], json.dumps(envelope))

    kind, handled_by = handled_by_for(fail_class)
    ctx.db.emit_event(
        ctx.thread_id, f"{kind} [{fail_class}]: {detail}", ctx.session_id,
        kind=kind, handled_by=handled_by,
    )

    return FailRecord(
        step=step, reason=detail, message=message, fail_class=fail_class, envelope=envelope,
    )
