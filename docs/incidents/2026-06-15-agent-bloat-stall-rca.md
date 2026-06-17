# RCA: Agent Context-Bloat Stall — 2026-06-15

**Session:** Juggle ZU / 2026-06-15  
**Scope:** Read-only investigation. No code changes.  
**Affected agents (known):** deafb983, 0a64d892, 7494e7b9, cb4ae1b6, 1e6e2455, 3306493b, b28153f2, 11bbdc03

---

## Executive Summary

Coder agents repeatedly stalled this session. The dominant failure is a self-reinforcing **alive-but-stalled-at-auto-compact-boundary** loop: an agent fills its 200k context window, the watchdog detects no pane change (stalled), calls `execute_recovery`, but `_classify_agent_state` always returns `alive_slow` because the Claude Code footer (`bypass permissions on`) is always in the scrollback — so the watchdog nudges the agent instead of replacing it. The nudge injects more tokens, triggers auto-compact, the agent loses context and sits at `❯ `. The loop repeats. The agent is **never decommissioned**. Secondary causes: (a) Claude Code "thinking" synonyms ("Befuddling", "Burrowing") are not in `_EXECUTION_MARKERS`, causing legitimate thinking turns to be misclassified as stalled; (b) the coder template gives no instruction to exit early at high context; (c) some agents called `integrate` synchronously, blocking for 6+ minutes.

---

## 1. Concrete Stall Mechanisms with Evidence

### Mechanism A: alive_slow Loop at Auto-Compact Boundary (PRIMARY — agents 7494e7b9, 3306493b)

**Evidence — snapshot `7494e7b9`:**
```
  5h[   18%    ]⏰46m | Sonnet 4.6(164.0k/200.0k) | ᓚᘏ-. Nano:dozing...
  1% until auto-compact · /model sonnet[1m]
  ⏵⏵ bypass permissions on (shift+tab to cycle)
❯
```
Agent at 164k/200k (82%), auto-compact imminent, at input prompt doing nothing.

**Evidence — snapshot `3306493b`:**
```
✶ Befuddling… (26s · ↓ 467 tokens · thought for 1s)
  ⏵⏵ bypass permissions on (shift+tab to cycle)
  Sonnet 4.6(164.0k/200.0k) | 1% until auto-compact
❯
```
Agent still thinking ("Befuddling") at 164k, but `_EXECUTION_MARKERS` only checks for `"Thinking"` (literal) — "Befuddling" is NOT matched. Watchdog classifies pane as stalled, fires recovery.

**The loop (code trace):**
1. `classify_pane_state` → `stalled` (content unchanged for threshold duration, "Thinking" not in tail)
2. `execute_recovery` (juggle_watchdog.py:456) → `_classify_agent_state(pane_content, pane_exists)`
3. `_classify_agent_state` (juggle_watchdog.py:293): checks `_CLAUDE_UI_MARKERS` — `"bypass permissions on"` **is always present** in every Claude Code pane footer → returns `"alive_slow"` unconditionally
4. Branch at juggle_watchdog.py:499-511: `alive_slow` → `nudge_and_notify(...)` → **RETURN** (no decommission, no fresh spawn)
5. `nudge_and_notify` sends: `Escape` + `_CONTINUE_INSTRUCTION` + `Enter` → injects ~50 tokens into a 164k-token context
6. Auto-compact triggers. Agent context collapses to ~50k. Agent loses all task state.
7. Agent sits at `❯ ` input prompt, confused or idle.
8. Next watchdog cycle: same classification → same nudge → cycle repeats.

**The core gap:** `alive_slow` path in `execute_recovery` has no context-level threshold. A 164k-context stalled agent and a 10k-context thinking agent both receive the same nudge. The former should be decommissioned.

---

### Mechanism B: Thinking-Synonym Gap — Legitimate Work Interrupted (agents 3306493b, 11bbdc03)

`_EXECUTION_MARKERS` (juggle_watchdog.py:65):
```python
_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")
```

`classify_pane_state` (juggle_watchdog.py:212):
```python
if "Thinking" in tail or stalled_for < 60:
    return "quiet", None
```

Claude Code uses many synonyms for its thinking indicator that rotate across responses: **"Befuddling", "Burrowing", "Sautéed", "Cooked", "Churned", "Brewed", "Baked", "Crunched"** (observed in snapshots). None of these match `"Thinking"`. An agent in a long thinking turn shows one of these synonyms but the watchdog classifies it as stalled after `threshold` seconds.

Result: the nudge interrupts a legitimate (multi-minute) thinking turn, which is fatal near the context limit.

---

### Mechanism C: Synchronous `integrate` Blocking (agent 11bbdc03)

**Evidence — snapshot `11bbdc03`:**
```
  Bash(cd /tmp/juggle-trading-edge-ZC && uv run juggle_cli.py integrate ZC 2>&1 | tail -40)
  ⎿  Running… (1m 24s)
     (ctrl+b ctrl+b (twice) to run in background)

· Burrowing… (6m 17s · ↓ 12.1k tokens)
```
Agent called `integrate` **synchronously** (not backgrounded). `integrate` ran 6+ minutes. Pane output stopped changing during that time. Watchdog classified as stalled. Recovery interrupted the running integrate.

The coder template gives no instruction to background `integrate`.

---

### Mechanism D: Post-Commit Idle — No `complete-agent` (agents deafb983, 1e6e2455)

**Evidence — snapshot `deafb983`:** Pane ends with:
```
❯ go ahead
```
The orchestrator had to type "go ahead" manually to unstick the agent. The agent had finished implementation and committed but did not call `complete-agent` — it sat waiting at the input prompt.

The `UNIVERSAL_PREAMBLE` says `"NEVER stop at the input prompt"` but agents drift here after committing, treating the commit as the final step. The template says "when finished, call: juggle complete-agent" but doesn't explicitly model `commit → complete-agent` as a required sequence.

---

### Mechanism E: Cold-Spawn Sit-at-Prompt (agent 0a64d892)

**Evidence — snapshot `0a64d892`:**
```
Sonnet 4.6(0/200.0k) | ᓚᘏ-ᗢ Byte:relaxed~
❯
```
Fresh recovery agent, 0 tokens used, sitting idle at input prompt. Either `send_task` never ran (the RuntimeError path in execute_recovery:682-722 was hit silently), or the cold-start trust prompt appeared and wasn't handled. The pane has no task content at all.

---

## 2. Hypothesis Verdict

### Hypothesis 1: Bad/Unclear Instructions — SECONDARY CAUSE

The coder template lacks three specific instructions that would have prevented stalls:
1. **No context-threshold early-exit rule** — agents have no instruction to commit + bail out when context is near full.
2. **No incremental commit mandate** — agents batch all work into one commit at the end, maximizing time-in-flight at high context.
3. **No integrate backgrounding rule** — agents call `integrate` synchronously, blocking for long periods.

The "NEVER stop at the input prompt" rule exists but is not specific enough to prevent post-commit idle (Mechanism D).

These are real gaps, but they are secondary. Even with perfect instructions, the `alive_slow` loop (Mechanism A) would still trap agents that reach high context for any reason.

### Hypothesis 2: Trajectory/Hang Mechanism — PRIMARY CAUSE

The watchdog has two architectural gaps that cause and amplify the stalls:

1. **`alive_slow` has no context-level exit clause.** Any agent with Claude Code UI markers in its scrollback (i.e., every interactive agent) is classified `alive_slow` and only nudged, never decommissioned — regardless of context level. At 164k/200k, the nudge makes things worse.

2. **`_EXECUTION_MARKERS` misses all Claude thinking synonyms except "Thinking".** This causes false-stall classification of agents mid-thought, leading to disruptive nudges and cascade failures near the context limit.

---

## 3. Does Recovery Re-Use Bloated Agents?

**YES — for alive_slow (covers all high-context stalls):**  
The `execute_recovery` function explicitly returns early for `alive_slow` without decommissioning (juggle_watchdog.py:501-511). The bloated agent receives a nudge and continues running. No fresh agent is spawned.

**NO — for dead/never_fired:**  
execute_recovery kills the old pane and spawns a fresh agent (juggle_watchdog.py:642: `new_agent = mgr.spawn_agent(...)`), then re-dispatches `last_task`. However: `last_task` is the full original prompt — for complex coder tasks this can be 5-10k tokens just for the preamble, giving the recovery agent a disadvantageous start.

---

## 4. Ranked Fixes

### Fix 1 ⭐ HIGHEST LEVERAGE — Context-threshold decommission in `execute_recovery` (CODE)

**Location:** `juggle_watchdog.py:execute_recovery`, around line 499  
**What:** Parse context `%` from pane content. If `alive_slow` AND context ≥ threshold (e.g. 80%), fall through to the decommission + fresh-spawn path instead of nudging.

```python
# After: agent_state = _classify_agent_state(pane_content, pane_exists)
if agent_state == "alive_slow":
    ctx_pct = _parse_context_pct(pane_content)  # new helper
    if ctx_pct is not None and ctx_pct >= _CONTEXT_RECYCLE_THRESHOLD:
        _log.warning(
            "Watchdog: alive_slow at high context (%.0f%%) — recycling to fresh agent",
            ctx_pct * 100,
        )
        # fall through to decommission + re-dispatch below
    else:
        nudge_and_notify(db, mgr, live, pane_content)
        return
```

`_parse_context_pct` parses the pattern `Sonnet X.X(NNNk/200.0k)` from pane tail (format is stable in CC). `_CONTEXT_RECYCLE_THRESHOLD = 0.80` (configurable via settings).

**Why code, not prompt:** An agent at 164k context cannot act on a prompt instruction — it's already stalled. This fix operates at the orchestrator level, outside the agent.

**Agent-verifiable test:** Mock pane content with `Sonnet 4.6(164k/200.0k)` + Claude UI markers → assert execute_recovery spawns a NEW agent (not just nudges).

---

### Fix 2 — Add thinking synonyms to `_EXECUTION_MARKERS` (CODE)

**Location:** `juggle_watchdog.py:65` and `:212`  
**What:** Expand the synonym list and/or use a regex that matches the CC thinking spinner pattern.

```python
_THINKING_RE = re.compile(
    r"\b(Thinking|Befuddling|Burrowing|Saut[ée]ed|Cooked|Churned|Brewed|Baked|Crunched?)\b"
)
```

In `classify_pane_state`:
```python
if _THINKING_RE.search(tail) or stalled_for < 60:
    return "quiet", None
```

**Why code:** These synonyms are structural (CC uses them as spinner text), not content. Maintaining a regex in code is robust; relying on prompts to work around it is fragile.

**Agent-verifiable test:** Parameterize test over all synonym strings, assert `classify_pane_state` returns `"quiet"` for each.

---

### Fix 3 — Coder template: context-threshold early-exit rule (PROMPT)

**Location:** `settings.json` → `task_templates.coder`  
**What:** Add explicit context-threshold instruction:

```
### Context Budget
If your context token count exceeds 150k (visible in the status bar as
`Sonnet X.X(NNNk/200.0k)`), IMMEDIATELY:
1. `git add -A && git commit -m "partial: <describe what's done>"`
2. `juggle complete-agent <thread> "PARTIAL: committed through <step>; context limit hit" --retain "<current state + next step"`
Do NOT continue working. A fresh agent will pick up from your commit.
```

**Why this ordering (Fix 3, not Fix 1):** Prompt-enforced rules are agent-side and can be forgotten or ignored at high context. Fix 1 (code-enforced recycling) catches the failure; Fix 3 prevents the failure. Both are needed, but Fix 1 works even when Fix 3 is forgotten.

---

### Fix 4 — Coder template: incremental commit + integrate backgrounding (PROMPT)

**Location:** `settings.json` → `task_templates.coder`  
**What:**  
```
### Commit Discipline
Commit after every RED→GREEN cycle (tests pass for a logical unit). Do NOT batch
all commits at the end. Small commits = short context exposure + watchdog-safe checkpoints.

### Long Commands
When running `integrate`, always background it:
  juggle integrate <thread>
(the CLI backgrounds automatically). Never run integrate synchronously — it can
block your pane for 5-10 minutes and trigger false-stall recovery.
```

---

### Fix 5 — Recovery sends compact task summary, not raw `last_task` (CODE, lower priority)

**Location:** `execute_recovery`, the `mgr.send_task(new_pane_id, last_task)` call  
**What:** For long tasks (>3k chars), inject a header: `"RECOVERY CONTEXT: This is a re-dispatch. Prior agent stalled at high context. Git commits may be partial. Resume from HEAD. Original task follows:\n\n"` + truncated task.  
**Effort:** Medium. Requires a truncation strategy.

---

## 5. Agent-Verifiable Checks per Fix

| Fix | Verifiable by agent? | How |
|-----|---------------------|-----|
| Fix 1 (context-recycle threshold) | Yes | Unit test: mock pane content with `164k/200k` + `bypass permissions on` → assert `execute_recovery` calls `spawn_agent` (not `nudge_and_notify`) |
| Fix 2 (thinking synonyms) | Yes | Parametric test: assert `classify_pane_state` returns `"quiet"` for each synonym word in tail |
| Fix 3 (context budget rule) | Partially | Manual: dispatch a dummy task to an agent, verify prompt includes the threshold rule |
| Fix 4 (commit + integrate) | Partially | Grep coder template in settings for the integrate instruction |
| Fix 5 (recovery summary) | Yes | Unit test: assert recovery prompt for 10k+ task starts with "RECOVERY CONTEXT:" header |

---

## 6. Summary Table

| # | Agent | Mechanism | Root Cause |
|---|-------|-----------|-----------|
| deafb983 | ZO coder | Post-commit idle (D) | No explicit `commit → complete-agent` sequence in template |
| 0a64d892 | ZK recovery | Cold-spawn sit-at-prompt (E) | send_task failure or cold-start trust prompt unhandled |
| 7494e7b9 | ZC coder | alive_slow loop at 164k (A) | execute_recovery never decommissions alive agents |
| 3306493b | YY coder | Thinking-synonym + alive_slow at 164k (A+B) | "Befuddling" not in _EXECUTION_MARKERS; nudge at auto-compact |
| 11bbdc03 | ZC coder | Synchronous integrate blocking (C) | No backgrounding instruction; integrate ran 6+ min |
| 1e6e2455 | ZC coder | Post-commit idle (D) | Same as deafb983 |
| b28153f2 | Orchestrator | Not an agent stall — user main session | N/A |

---

## Key Takeaway

The highest-leverage single fix is **code-enforced context-recycling** in `execute_recovery` (Fix 1): decommission alive agents above 80% context and spawn a fresh one with the last task. This closes the loop that kept all session agents alive-but-stalled for 30+ minutes each. Prompt fixes (3, 4) prevent agents from reaching the boundary in the first place, but Fix 1 is the safety net when they do.
