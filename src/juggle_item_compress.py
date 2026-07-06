"""Concise cockpit item formatter — compress verbose action items /
notifications to the canonical cockpit budget at the WRITE seam.

Incident 2026-07-05: cockpit action items (the ``[auto-decision]`` verbatim
capture) and completion notifications (the agent's raw ``agent complete``
string) rendered as multi-paragraph dumps, blowing the ~280-char / 2-5-line
budget and making the Action Items + Notifications panels unreadable.

``compress_item(kind, label, text)`` collapses over-budget free text to:
  - ACTION       — <=5 lines, structure ``what · risk · fix · → Rec: <ask>``
  - NOTIFICATION — exactly 1 line, <=280 chars, leading ``✓``

Design (vault docs 2026-07-05-concise-cockpit-item-formatter.md, option C):
  1. Short items (already within budget) pass through untouched.
  2. Over-budget free text → few-shot LLM compressor (``_cheap_llm_call``),
     then a constraint enforcer that repairs/truncates any budget violation.
  3. FAIL-OPEN: no API key / LLM error / empty output → deterministic truncate
     to budget + a "see thread messages <ref>" pointer. NEVER raises, NEVER
     blocks item creation. Full detail always stays in thread messages.
"""
from __future__ import annotations

import logging
import os
import re

# --- Budget constants -------------------------------------------------------
ACTION_MAX_LINES = 5
NOTIFICATION_MAX_LINES = 1
NOTIFICATION_MAX_CHARS = 280
NOTIFICATION_MARKER = "✓"
ACTION_HEADER_MARKER = "⚡"
# A char cap used only to DECIDE whether free text needs compressing (both
# kinds). The action OUTPUT itself is bounded by lines, not chars.
_OVERFLOW_CHARS = NOTIFICATION_MAX_CHARS


# --- Few-shot prompt (both canonical examples baked in verbatim) ------------
_FEWSHOT_PROMPT = """\
SYSTEM: You compress a verbose Juggle status/decision writeup into ONE cockpit item.
Output ONLY the compressed item — no preamble, no code fences.

Rules:
- ACTION (a decision/action is needed): ≤5 lines. Structure:
    line 1: "⚡ [<label>] <short title ending in the question/ask>"
    middle: what happened · the risk/why it matters · the fix/option (keep concrete IDs, shas, counts)
    last line: "→ Rec: <recommendation>. <the ask, e.g. 'Ack to proceed.'>"
- NOTIFICATION (FYI/done, no action): exactly 1 line, ≤280 chars:
    "✓ [<label>] <what happened> — <key evidence (shas, pass counts)>"
- Plain English. Drop narrative and process. Keep numbers/IDs. Never invent facts.

EXAMPLE 1 (ACTION)
Kind: action  Label: SL
Input: <the full multi-paragraph PR #62 review writeup>
Output:
⚡ [SL] PR #62 weekly-autofix — merge?
Necessary: cleans 22 real F401/F841 findings. Not clean as-is — FX-1 dropped import time from juggle_cockpit.py → 2 cockpit patch-target tests fail (rest verified safe).
Fix = 1 line (import time # noqa), re-verified 4132 pass.
→ Rec: fix + ff-merge. Ack to proceed.

EXAMPLE 2 (NOTIFICATION)
Kind: notification  Label: SL
Input: <the full fix-agent completion writeup>
Output:
✓ [SL] PR #62 (autofix) merged @ ff9f051 — 22 lint findings cleaned; caught + fixed 1 regression (import time). 4132 pass.

NOW COMPRESS
Kind: {kind}  Label: {label}
Input: {text}
Output:"""


def _cheap_llm_call(prompt: str, timeout: int = 15) -> str | None:
    """Thin, monkeypatchable seam over juggle_cli_common._cheap_llm_call.

    Imported lazily to avoid a circular import (this module is imported by the
    hook / completion write paths, which also pull in juggle_cli_common)."""
    from juggle_cli_common import _cheap_llm_call as _impl

    return _impl(prompt, timeout=timeout)


def _has_llm_key() -> bool:
    """True when an LLM key is present. The autofix container has neither — it
    must still create items, so a missing key routes to deterministic fallback."""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENROUTER_KEY")
    )


def is_over_budget(kind: str, text: str) -> bool:
    """True when free text exceeds the kind's budget and needs compressing."""
    if not text:
        return False
    lines = text.splitlines()
    if kind == "notification":
        return len(lines) > NOTIFICATION_MAX_LINES or len(text) > NOTIFICATION_MAX_CHARS
    return len(lines) > ACTION_MAX_LINES or len(text) > _OVERFLOW_CHARS


def compress_item(
    kind: str, label: str, text, thread_ref: str | None = None
) -> str:
    """Compress an over-budget cockpit item to its canonical format.

    kind: "action" | "notification". label: the thread label (e.g. "SL").
    thread_ref: what the deterministic-fallback pointer names (defaults to
    label). Short items pass through untouched. FAIL-OPEN: any LLM failure or
    a missing key yields a deterministic truncate-with-pointer — never raises.
    """
    text = (text or "").strip()
    if not text or not is_over_budget(kind, text):
        return text

    source = text
    if _has_llm_key():
        llm = _try_llm(kind, label, text)
        if llm:
            source = llm

    if kind == "notification":
        return _enforce_notification(label, source, thread_ref or label)
    return _enforce_action(label, source, thread_ref or label)


def _try_llm(kind: str, label: str, text: str) -> str | None:
    """Run the few-shot compressor. Returns None on any error (fail-open)."""
    try:
        prompt = _FEWSHOT_PROMPT.format(kind=kind, label=label, text=text)
        out = _cheap_llm_call(prompt, timeout=15)
    except Exception as exc:  # network/binary/timeout — never propagate
        logging.warning("compress_item LLM call failed: %s", exc)
        return None
    out = (out or "").strip()
    # Strip a stray code fence the model may wrap the answer in.
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", out).strip()
    return out or None


# --- Constraint enforcers (repair LLM output AND deterministic fallback) -----


def _enforce_notification(label: str, text: str, thread_ref: str) -> str:
    """Guarantee: exactly 1 line, <=280 chars, leading ✓. Idempotent on valid
    input; truncates-with-pointer on overflow."""
    line = " ".join(text.split())  # collapse ALL whitespace → single line
    if not line.startswith(NOTIFICATION_MARKER):
        line = f"{NOTIFICATION_MARKER} [{label}] {line}"
    if len(line) <= NOTIFICATION_MAX_CHARS:
        return line
    pointer = f" … (see thread messages {thread_ref})"
    keep = NOTIFICATION_MAX_CHARS - len(pointer)
    return line[:keep].rstrip() + pointer


def _enforce_action(label: str, text: str, thread_ref: str) -> str:
    """Guarantee: <=5 lines with a "→ Rec:" line. Idempotent on valid input;
    truncates + synthesizes a rec pointer on overflow."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        lines = [text.strip()]

    # Ensure a leading "⚡ [label]" header line.
    if not lines[0].startswith(ACTION_HEADER_MARKER):
        lines.insert(0, f"{ACTION_HEADER_MARKER} [{label}]")

    rec_idx = next(
        (i for i, ln in enumerate(lines) if "→" in ln or "Rec:" in ln), None
    )
    if rec_idx is None:
        # No rec line — synthesize one that doubles as the detail pointer.
        rec = f"→ Rec: see thread messages {thread_ref}."
        body = lines[: ACTION_MAX_LINES - 1]
        return "\n".join(body + [rec])

    if len(lines) <= ACTION_MAX_LINES:
        return "\n".join(lines)
    # Overflow: keep the header + leading body, then the rec line last.
    rec_line = lines[rec_idx]
    head = [ln for i, ln in enumerate(lines) if i != rec_idx][: ACTION_MAX_LINES - 1]
    return "\n".join(head + [rec_line])
