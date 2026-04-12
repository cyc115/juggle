# Proactive Orchestrator — Design Spec

**Date:** 2026-04-11  
**Status:** Approved  
**Scope:** `commands/start.md` prompt template changes only — no DB, no CLI, no new roles.

---

## Problem

The orchestrator relies on the user to request verification after implementation. Coder agents are trusted blindly — they call `complete-agent` without running tests or checking quality. High-level tasks require multiple user nudges to reach a correct result.

## Goal

The orchestrator takes a high-level requirement and drives it to completion autonomously:
- Planner captures how to verify success
- Coder verifies its own work before reporting done
- Coder self-corrects up to a project-specified retry limit
- Orchestrator surfaces clean pass/fail with details — no user nudging needed

---

## Design

### What fits in Juggle

Juggle's responsibility is session and agent orchestration — not code quality. The verify+fix loop belongs to the coder agent, not the orchestrator. Juggle's role here is:

1. Ensure the planner always produces verification instructions
2. Ensure the coder always consumes and executes them
3. Ensure failure is surfaced clearly, not silently swallowed

No new DB columns. No new CLI commands. No new agent roles.

---

### Change 1: Planner prompt template

Every plan file must include a `## Verification` section. The planner discovers verification commands by reading:
- `CLAUDE.md` (project-specific instructions)
- `README` / `README.md`
- `pyproject.toml`, `setup.cfg`, `package.json`, `Makefile`

If no project-specific commands are found, use sensible defaults (e.g. `pytest`, `ruff check`, `mypy`).

**Required section in every plan file:**

```markdown
## Verification
commands:
  - <test runner command>
  - <lint/type-check command>
  - <smoke test command if applicable>
  - <any project-specific checks>
max_retries: <1 for simple tasks, 2–3 for complex>
success_criteria: <one sentence — what passing looks like>
```

`max_retries` guidance for planner:
- 1: single-file change, well-understood area
- 2: multi-file change, moderate complexity
- 3: cross-cutting change, new feature, high risk

---

### Change 2: Coder prompt template

After implementing, coder must execute the verify+fix loop before calling `complete-agent`.

**Verification protocol (append to every coder task prompt):**

```
After implementing:
1. Read the ## Verification section from the plan file.
2. Run each command. Capture output.
3. If all pass: call complete-agent with "Done. All checks pass."
4. If any fail: fix the failures and re-run. Repeat up to max_retries times.
5. If still failing after max_retries:
   call complete-agent with "PARTIAL: <what passed> | FAILED: <what failed and why>"
```

The orchestrator reads the `complete-agent` result at next user message and surfaces failures:
```
[Topic X] ⚠️ Incomplete — <what failed>. /juggle:resume-topic X to review.
```

---

## Out of Scope

- Separate verifier role / Phase 3 in orchestrator protocol
- DB columns: `verify_attempts`, `max_retries`, `verify_commands`, `last_verify_error`
- CLI commands: `set-verify-config`, `record-verify-result`, `get-verify-config`
- Orchestrator-managed retry loop (loop lives inside the coder agent)

---

## Files Changed

| File | Change |
|---|---|
| `commands/start.md` | Add `## Verification` requirement to planner prompt template |
| `commands/start.md` | Append verification protocol block to coder prompt template |

No other files change.

---

## Success Criteria

- Planner always includes `## Verification` section with runnable commands
- Coder runs all verification commands before calling `complete-agent`
- Coder self-corrects up to `max_retries` without user input
- On failure, orchestrator surfaces structured pass/fail at next natural pause
- No new DB schema, CLI subcommands, or agent roles introduced
