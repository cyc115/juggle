# Agent Prompt Injection — Design Spec

**Date:** 2026-05-16  
**Status:** Implemented 2026-05-16 (v1.20.0)  
**Scope:** Per-role behavioral guardrails for researcher / coder / planner agents

---

## Problem

Juggle's agent context injection (`juggle_context.py`) provides strong state information (thread summary, Q&A history, key decisions) but zero per-role behavioral guidance. Agents have distinct failure modes:

- **Researcher:** hallucinate citations, loop indefinitely, produce uncited assertions
- **Coder:** over-engineer, touch unrelated code, skip self-validation
- **Planner:** produce ambiguous specs, skip devil's advocate, hand off weak plans

Research confirms the fix: inject role identity + behavioral constraints before the agent starts, and embed output format + stop criteria in every dispatched task. Token cost is acceptable (~75 tokens per turn for context anchor; task-level specs are one-time cost).

---

## Approach: Two-Layer Injection

### Layer 1 — Context Anchor (juggle_context.py, ~150 chars, every turn)

Short persistent block injected when `JUGGLE_IS_AGENT=1`:
- Role identity (1 sentence)
- Completion signal reminder

Keeps the agent oriented across multi-turn tasks without significant token overhead.

### Layer 2 — Task Behavioral Spec (task file, one-time)

Full behavioral spec embedded in each dispatched task file:
- Search strategy / stop criteria (researcher)
- Scope constraint + quality gate (coder)
- Decomposition + DA gate + spec format (planner)

This is richer and task-specific. Resets on new tasks (acceptable — new tasks bring new instructions).

---

## Implementation

### 1. `juggle_context.py` — Role anchor injection

**Role detection:**  
When `JUGGLE_IS_AGENT=1`, read `JUGGLE_AGENT_ROLE` env var (set at spawn time by `juggle_tmux.py`). If unset, skip injection (graceful degradation).

**Injected block (appended after thread context, before char limit):**

```
--- AGENT ROLE ---
ROLE: {role}. {role_identity_sentence}
COMPLETION: python3 {PLUGIN_ROOT}/src/juggle_cli.py complete-agent <THREAD> "<summary>" --retain "<key finding>"
```

Role identity sentences (stored in `juggle_settings.py` under `agent.role_context`):
- `researcher`: "Produce comprehensive, well-structured, cited reports. Never fabricate URLs."
- `coder`: "Implement exactly what is specified — no more. Minimal diff."  
- `planner`: "Produce plans a coder can execute without clarification."

**Token budget:** ~150 chars = ~19 tokens. Negligible.

**Required change in `juggle_tmux.py`:**  
Set `JUGGLE_AGENT_ROLE={role}` in the env prefix when launching Claude:
```python
cmd = f"env -u CLAUDE_PLUGIN_DATA JUGGLE_IS_AGENT=1 JUGGLE_AGENT_ROLE={role} {cmd}"
```

### 2. `juggle_settings.py` — Role context config

Add to `agent` section (config-overridable):

```python
"role_context": {
    "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
    "coder":      "Implement exactly what is specified — no more. Minimal diff.",
    "planner":    "Produce plans a coder can execute without clarification.",
},
```

### 3. `commands/research.md` — Researcher task spec

Add a `## Researcher behavioral spec` block to every dispatched task file, immediately after `Research topic:`:

```
## Researcher behavioral spec

CONFIDENCE MARKERS (apply to every claim):
- [HIGH CONFIDENCE] — 3+ independent sources agree
- [CONFLICTING] — sources disagree; surface the conflict explicitly
- [UNVERIFIED] — single source or unverifiable

Never fabricate URLs. State gaps explicitly rather than guessing.

OUTPUT FORMAT:
## Summary (3-5 sentences, standalone readable)
## [Section per research angle]
  - Finding [CONFIDENCE] — URL
## Gaps / open questions
```

**DA findings incorporated:**
- Removed the SEARCH STRATEGY block (search ordering and stop criteria are owned by the round-based structure already defined in `research.md` — the two were conflicting, see DA review 2026-05-16).
- Kept CONFIDENCE MARKERS and OUTPUT FORMAT — these are the high-value, non-conflicting parts.

### 4. `commands/delegate.md` (or equivalent coder/planner task dispatch)

**Coder task prefix block:**

```
## Coder behavioral spec

SCOPE: Only change what the task requires. Do not refactor, add comments, or improve
surrounding code. If requirements are ambiguous, STOP and signal via complete-agent
with "BLOCKED: <question>" before making assumptions.

QUALITY GATE (run before complete-agent):
1. Run tests for changed files (if tests exist)
2. Fix linting errors
3. Fix type errors
4. Verify diff has no unrelated changes
5. Invoke mike:pre-pr skill (configurable via agent.quality_gate_skill setting)

VERSION BUMP: patch=fix, minor=feature, major=breaking. State target version in summary.
```

**Planner task prefix block:**

```
## Planner behavioral spec

DECOMPOSE: Break into subtasks of one file/concern each, ordered by dependency.
Each subtask must have: what to do, where to do it, acceptance criteria.

DEVIL'S ADVOCATE (mandatory before emitting plan):
1. Identify weakest assumption and its failure mode
2. Ask: is there a simpler alternative that achieves the same goal?
3. Hunt for hidden dependencies or scope creep
State findings in ## Devil's Advocate section of plan.

DONE when: a coder with no prior context could execute every subtask without asking.
```

---

## Settings Schema Addition

```python
"agent": {
    # ... existing keys ...
    "role_context": {
        "researcher": "Produce comprehensive, well-structured, cited reports. Never fabricate URLs.",
        "coder":      "Implement exactly what is specified — no more. Minimal diff.",
        "planner":    "Produce plans a coder can execute without clarification.",
    },
    "quality_gate_skill": "mike:pre-pr",  # invoked by coder before complete-agent
},
```

---

## Files Changed

| File | Change |
|------|--------|
| `src/juggle_context.py` | Add `_render_agent_role_anchor()`, call when `JUGGLE_IS_AGENT=1` |
| `src/juggle_tmux.py` | Set `JUGGLE_AGENT_ROLE={role}` in env prefix at spawn |
| `src/juggle_settings.py` | Add `agent.role_context`, `agent.quality_gate_skill` |
| `commands/research.md` | Embed researcher behavioral spec in task file template |
| `commands/delegate.md` | Embed coder + planner behavioral specs in task file templates |

---

## What's Out of Scope

- Few-shot examples in prompts (high impact but requires content decisions per role — separate effort)
- LLM-as-a-Judge post-evaluation of research output
- Automatic confidence marker verification

---

## Devil's Advocate Findings

| Assumption | Failure mode | Mitigation |
|-----------|-------------|-----------|
| JUGGLE_AGENT_ROLE env var is always set | Env not propagated → no injection | Graceful: skip injection if var missing |
| "3 consecutive no-new-URL" is detectable | Agent can't reliably track this without code | Fallback: 15-search ceiling in text; agent judgment |
| mike:pre-pr is available to all coders | It's a personal skill, not in juggle repo | Made configurable via `quality_gate_skill` setting |
| Planner DA gate adds latency | +1-2 turns per plan | Documented trade-off; quality > speed for planning |

---

## Success Criteria

1. Researcher tasks produce output with `[HIGH CONFIDENCE]` / `[CONFLICTING]` markers
2. Researcher output consistently has `## Gaps / open questions` section
3. Coder tasks include quality gate invocation before `complete-agent`
4. Planner tasks include `## Devil's Advocate` section in every plan
5. Context injection adds `--- AGENT ROLE ---` block in agent panes (verify via `get-context`)
