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
`integrate_failed` with `handled_by='watchdog'` for now.

T3 (spec rev 6 §4): signature-keyed retry caps + novelty gates, layered onto
`record_refusal` without touching `handled_by_for`'s pinned (kind, handled_by)
mapping for a bare fail_class — `compute_signature()` fingerprints a failure
(class + sorted conflicting files/tests); `check_retry_policy()` decides,
against the PRIOR envelope's counters, whether a repair may be dispatched:
repair cap 1/signature, backstop 3/topic (checked first — overrides even a
free trunk/head-moved blind retry), and a trunk/head move since the prior
attempt grants a free retry that consumes no attempt. A breach of either cap
overrides the routine class's routing to `repair_exhausted`/orchestrator.
`machinery` never enters the retry policy — it is never auto-repaired.
"""

from __future__ import annotations

import hashlib
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
# Requirement #2 (2026-07-19 KF/KH/KG clobber incident): submit() reported the
# ff-merge landed, but the branch's commits are NOT confirmed as an ancestor
# of main by the time cleanup would run — a pipeline-defect backstop, never
# auto-repaired (same as the other MACHINERY steps).
STEP_MERGE_NOT_CONFIRMED = "merge_not_confirmed"

_STEP_TO_CLASS = {
    STEP_DIRTY_WORKTREE: COLLISION,
    STEP_NO_MAIN_BRANCH: MACHINERY,
    STEP_EMPTY_BRANCH: MACHINERY,
    STEP_REBASE_CONFLICT: CONFLICT,
    STEP_TEST_FAILURE: RED_SUITE,
    STEP_SUBMIT_FAILED: DIVERGENCE,
    STEP_UNEXPECTED: MACHINERY,
    STEP_MERGE_NOT_CONFIRMED: MACHINERY,
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


# ── T3: signature-keyed retry caps + novelty gates (spec rev 6 §4) ──────────

REPAIR_CAP_PER_SIGNATURE = 1
BACKSTOP_TOTAL_PER_TOPIC = 3


def compute_signature(fail_class: str, files_or_tests: list[str]) -> str:
    """sha1(class + sorted fingerprint) — a new signature is a new problem."""
    payload = fail_class + "|" + "|".join(sorted(files_or_tests or []))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def register_attempt(envelope: dict, signature: str) -> dict:
    """Increment attempts_by_signature[signature] and attempts_total in-place."""
    by_sig = envelope.setdefault("attempts_by_signature", {})
    by_sig[signature] = by_sig.get(signature, 0) + 1
    envelope["attempts_total"] = envelope.get("attempts_total", 0) + 1
    return envelope


class RetryDecision:
    """allowed + reason + whether a repair attempt should be counted against
    the caps (a free trunk/head-moved blind retry does not)."""

    def __init__(self, allowed: bool, reason: str, consumes_attempt: bool):
        self.allowed = allowed
        self.reason = reason
        self.consumes_attempt = consumes_attempt

    def __repr__(self) -> str:
        return (
            f"RetryDecision(allowed={self.allowed!r}, reason={self.reason!r}, "
            f"consumes_attempt={self.consumes_attempt!r})"
        )


def check_retry_policy(
    prior_envelope: dict,
    signature: str,
    *,
    trunk_at_attempt,
    head_at_attempt,
    current_trunk,
    current_head,
) -> RetryDecision:
    """Novelty-gated retry decision against the PRIOR envelope's counters.

    Order matters: the 3-total backstop is checked FIRST — it overrides even
    a free trunk-moved blind retry, otherwise flapping signatures could retry
    forever every time trunk advances. Only once under the backstop, and only
    when there WAS a prior attempt (``trunk_at_attempt`` set — the very first
    attempt for a topic is never a "blind retry"), does a trunk/head move
    since that prior attempt grant a free, attempt-free blind retry;
    otherwise the per-signature cap (1) applies.
    """
    total = prior_envelope.get("attempts_total", 0)
    if total >= BACKSTOP_TOTAL_PER_TOPIC:
        return RetryDecision(
            False, "backstop: 3 total repairs per topic reached", consumes_attempt=False
        )

    if trunk_at_attempt is not None and (
        trunk_at_attempt != current_trunk or head_at_attempt != current_head
    ):
        return RetryDecision(
            True, "trunk/head moved since failed attempt — free blind retry",
            consumes_attempt=False,
        )

    by_sig = prior_envelope.get("attempts_by_signature", {})
    if by_sig.get(signature, 0) >= REPAIR_CAP_PER_SIGNATURE:
        return RetryDecision(
            False, f"repair cap reached for signature {signature}", consumes_attempt=False
        )

    return RetryDecision(True, "fresh attempt for signature", consumes_attempt=True)


def _prior_envelope(task: dict | None) -> dict:
    raw = (task or {}).get("fail_envelope")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def record_refusal(step: str, detail: str, ctx: FailContext) -> FailRecord:
    """R2+T2+T3: the DB bookkeeping half of a refused integrate.

    Writes the pinned prose action item (message text UNCHANGED from before
    the extraction), then (T2) persists the fail envelope onto the bound
    graph task and emits the routed event. For routine (non-machinery)
    classes, T3's novelty-gated retry policy decides whether the class's
    default `integrate_failed`/watchdog routing (a repair is dispatchable)
    is overridden to `repair_exhausted`/orchestrator (cap or backstop
    breached — STOP, this topic needs judgment, not another blind repair).
    Control flow (lock release, early return) stays in `_fail`.
    """
    message = f"⚠️ integrate failed [{ctx.worktree_branch}]: {detail}"
    # _once dedups a re-driven gate's byte-identical HIGH item (fix-conflict-envelope-routing).
    ctx.db.add_action_item_once(
        thread_id=ctx.thread_id, message=message, type_="manual_step", priority="high",
    )

    fail_class = classify(step, detail)
    envelope = build_envelope(fail_class, ctx)
    kind, handled_by = handled_by_for(fail_class)

    # T4 reads this flag to decide whether the failed topic gets a repair agent.
    # machinery is never auto-repaired; a routine class is dispatchable exactly
    # when the retry policy allows a fresh attempt (a cap/backstop breach flips
    # it False AND escalates below).
    dispatchable = False
    if fail_class != MACHINERY:
        prior = _prior_envelope(ctx.task)
        signature = compute_signature(fail_class, ctx.files)
        envelope["signature"] = signature
        envelope["attempts_by_signature"] = dict(prior.get("attempts_by_signature") or {})
        envelope["attempts_total"] = prior.get("attempts_total", 0)

        decision = check_retry_policy(
            prior, signature,
            trunk_at_attempt=prior.get("trunk_at_attempt"),
            head_at_attempt=prior.get("head_at_attempt"),
            current_trunk=ctx.trunk_at_attempt,
            current_head=ctx.head_at_attempt,
        )
        if decision.consumes_attempt:
            register_attempt(envelope, signature)
        if not decision.allowed:
            kind, handled_by = "repair_exhausted", "orchestrator"
        dispatchable = decision.allowed
    envelope["repair_dispatchable"] = dispatchable

    # Persist on the bound task, else on the topic bound to the thread (T4) —
    # a non-graph thread persists nothing. One helper owns the task-vs-topic
    # decision so this module stays a thin policy layer.
    from dbops.db_topics_marking import store_fail_envelope

    store_fail_envelope(ctx.db, ctx.thread_id, ctx.task, json.dumps(envelope))

    ctx.db.emit_event(
        ctx.thread_id, f"{kind} [{fail_class}]: {detail}", ctx.session_id,
        kind=kind, handled_by=handled_by,
    )

    return FailRecord(
        step=step, reason=detail, message=message, fail_class=fail_class, envelope=envelope,
    )
