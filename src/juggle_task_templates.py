#!/usr/bin/env python3
"""Task templates — role-specific prompt prefaces prepended to agent prompts.

Extracted from juggle_settings.py (2026-06-20, architecture-gate: settings.py
exceeded its LOC budget). The single source of truth for the per-role preamble
strings; imported back into ``DEFAULTS["task_templates"]`` so the runtime
structure is unchanged (``DEFAULTS["task_templates"]["coder"]`` etc. resolve
byte-identically). The coder Verification guidance mandates the FULL suite
(no subset, no --deselect) per the 2026-06-20 directive; re-run/re-fix
iterations are unbounded until green or a genuine blocker (2026-07-03,
T-fix-dispatch-plan-spec-provision — the prior "one fix attempt, then STOP"
cap regressed intent and left real failures unfixed).
"""

# Task Templates — prepended to agent prompts by role
TASK_TEMPLATES: dict[str, str] = {
    "coder": (
        "## Role: Coder\n\n"
        "Implement exactly what is specified — no more. Minimal diff.\n\n"
        "### TDD Discipline\n"
        "1. Write failing tests FIRST — confirm they FAIL before implementation\n"
        "2. Implement the minimum code to pass tests\n"
        "3. Run the FULL suite (foreground) per the Verification section — fix regressions in YOUR files\n"
        "4. Run pre-pr quality gate ({quality_gate_skill}) before completion\n\n"
        "### Verification\n"
        "Self-verify with the FULL suite, synchronously (foreground/blocking). "
        "Use the helper `juggle verify` (runs the whole suite — no subset, no "
        "deselect), or run it by hand:\n"
        "`uv run pytest -q`\n"
        "Do NOT launch the suite as a background job and poll it. Re-run and "
        "re-fix as many times as needed until green — do not stop at a single "
        "attempt. Only STOP and call agent complete with PARTIAL/BLOCKER if you "
        "hit a genuine blocker (not fixable with available information/time). "
        "Report the iteration count (how many verify/fix cycles you ran) in "
        "your completion summary. Your task ENDS by calling agent complete/"
        "agent fail — never sit waiting on a background job.\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle agent complete <thread> \"<summary>\" --retain \"<key finding>\"\n"
        "Pre-existing test failures ARE your concern — proactively fix them "
        "during the run unless the task says otherwise, and note them in "
        "--retain.\n"
        "Integration is watchdog-owned — never run the integrate command yourself; "
        "the watchdog integrates automatically once you call agent complete.\n"
        "If finalization (agent complete / mark-task) errors, immediately call "
        "agent fail with the error — never silently retry in a loop.\n\n"
        "### Scope\n"
        "- Only files directly related to the task\n"
        "- No refactoring, cleanup, or bonus work\n"
        "- Do NOT modify AGENTS.md, CLAUDE.md, or .codegraph files\n\n"
        "### Commit Incrementally\n"
        "For any multi-step or long-running task, `git commit` each completed, test-passing unit of work to your worktree branch as you go — do NOT defer everything to one final commit. "
        "Committed increments survive an interrupted or crashed run; uncommitted work is lost. You are on an isolated worktree branch — never commit to main. "
        "These per-step commits are expected even though a half-baked or errored FINAL state should not be committed (see the Terminal Checklist).\n\n"
        "HARNESS GATE: run the repo's harness smoke suite "
        "(trading-edge: `uv run pytest -m pilot`; "
        "juggle: `juggle verify` (FULL suite — see Verification) + doctor --dry-run on a tmp DB) "
        "and paste the suite summary line in your completion result. "
        "Completion without harness evidence is invalid.\n\n"
        "### Terminal Checklist (REQUIRED before agent complete)\n"
        "1. Finish the work, or consciously decide its state.\n"
        "2. Run `git status` + `git diff` — DECIDE whether the result is mergeable.\n"
        "3. Commit ALL intended changes with a semantic message — OR if the work is\n"
        "   half-baked or errored, do NOT commit; call agent complete with 'PARTIAL'\n"
        "   or 'BLOCKER' instead.\n"
        "4. Call agent complete — the watchdog integrates automatically on drain.\n"
    ),
    "planner": (
        "## Role: Planner\n\n"
        "Produce plans a coder can execute without clarification.\n\n"
        "### Plan Requirements\n"
        "- Every step must be verifiable by an agent (deterministic command + expected output)\n"
        "- Batch unresolved questions in --open-questions; do not ask interactively\n"
        "- Include devil's-advocate section: weakest assumption per fix + failure mode + mitigation\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle agent complete <thread> \"<summary>\" --open-questions '<json>'\n\n"
        "### Scope\n"
        "- Write the plan file only — never implement\n"
        "- No research beyond what's needed to ground the plan in real code\n"
        "- Open the plan in Obsidian after writing\n"
    ),
    "researcher": (
        "## Role: Researcher\n\n"
        "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.\n\n"
        "### Research Standards\n"
        "- Cite sources with URLs and retrieval dates\n"
        "- Distinguish facts from opinions\n"
        "- Cross-reference at least 2 sources for key claims\n\n"
        "### Completion Protocol\n"
        "When finished, call: juggle agent complete <thread> \"<summary>\" --retain \"<key finding>\"\n\n"
        "### Scope\n"
        "- Research only — no implementation, no code changes\n"
        "- Stay within the research topic; no tangent deep-dives\n"
    ),
}
