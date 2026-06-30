# Agent Topic Context: Do Agents Get All Topics?

**Date:** 2026-06-05  
**Verdict: H1 — agents already receive NO topic injection. Nothing to optimize.**

---

## What Agents Actually Receive

**Branch point — `juggle_hooks.py:256`:**
```python
if os.environ.get("JUGGLE_IS_AGENT") == "1":
    anchor = build_context_string()   # → _render_agent_role_anchor() ONLY
    if anchor:
        print(json.dumps({"hookSpecificOutput": {"additionalContext": anchor}}))
    sys.exit(0)
```
The hook exits immediately after printing the role anchor. No DB query, no thread list, no action items, no autopilot directive, no auto-approve sweep.

**Context builder — `juggle_context.py:161`:**
```python
def _build(db: JuggleDB) -> str:
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        return _render_agent_role_anchor()   # ← EARLY RETURN, nothing else
    ...
    # all of: action items, Tier 1 threads, Tier 2 threads, notifications
    # is built ONLY if we reach here (i.e., orchestrator session)
```

Comment at `juggle_context.py:154–160` (existing, written by a prior implementor):
> "Agent sessions get ONLY their role anchor — never the orchestrator dashboard. The 'JUGGLE ACTIVE' block is orchestrator-only context (explicitly tagged 'do not forward to sub-agents') and an agent is told to ignore all of it, so injecting it wastes up to context_injection_char_limit (~2000 tokens) per task prompt on every dispatched agent. Returned before any DB query so it costs nothing and needs no active orchestrator."

**The role anchor** (`juggle_context.py:141–144`):
```
--- AGENT ROLE ---
ROLE: {role}. {identity string from settings}
COMPLETION: python3 {plugin_root}/src/juggle_cli.py agent complete <THREAD> "<summary>" --retain "<key finding>"
```
Approximately 150–250 chars (~40–65 tokens). Contains zero topics, threads, summaries, or Q&A history.

---

## All Five Hook Guards (complete map)

| Hook | Agent guard | Location | Effect |
|---|---|---|---|
| `UserPromptSubmit` | `if JUGGLE_IS_AGENT == "1": ... sys.exit(0)` | `juggle_hooks.py:256–273` | Prints role anchor only; skips DB, autopilot, auto-approve |
| `SessionStart` | `if JUGGLE_IS_AGENT == "1": sys.exit(0)` | `juggle_hooks.py:405–406` | No startup topics tree, no Hindsight recalls |
| `PreCompact` | `if os.environ.get("JUGGLE_IS_AGENT"): sys.exit(0)` | `juggle_hooks.py:533–534` | No checkpoint written |
| `PreToolUse` | `if os.environ.get("JUGGLE_IS_AGENT"): _log_agent_tool_use(data); sys.exit(0)` | `juggle_hooks.py:632–634` | Logs tool telemetry only; no blocking |
| `PostToolUse` | `if os.environ.get("JUGGLE_IS_AGENT") == "1": sys.exit(0)` | `juggle_hooks.py:736–737` | No orchestrator-violation warnings |

**Extra safety net** at `juggle_hooks.py:821–834`: `PostToolUse` detects if the orchestrator's `Agent` tool prompt contains `"JUGGLE ACTIVE"` and fires a notification warning the orchestrator that it leaked the dashboard to a sub-agent.

---

## Token Cost Quantification

| Context | Chars | ~Tokens | Who receives |
|---|---|---|---|
| Role anchor | ~200 | ~50 | Every agent, every turn |
| Orchestrator dashboard | up to 8,000 (`context_injection_char_limit`) | ~2,000 | Orchestrator only |
| Topics tree (startup) | varies, unbounded | 500–2,000+ | Orchestrator only (SessionStart) |

The dashboard cap is `juggle_settings.py:28`: `"context_injection_char_limit": 8000`.  
**Agents save ~2,000 tokens/turn** compared to if they received the full dashboard. With 10 tool calls per task and 5 concurrent agents this would have been ~100k tokens/session wasted — already avoided.

---

## Hypothesis Verdicts

**H1: Agents already get NO topic injection** → ✅ **CONFIRMED**  
The early-return at `juggle_context.py:161` and the hook exit at `juggle_hooks.py:273` make this ironclad. The code comment at `juggle_context.py:154` proves intent is explicit, not accidental.

**H2: Agents get ALL topics → project-scoping is the right fix** → ❌ **DOES NOT APPLY**  
Agents get zero topics. Project-scoping would be a no-op on nothing.

**H3: Agents get all topics AND it's load-bearing** → ❌ **DOES NOT APPLY**  
No agent code reads the dashboard block. The `agent complete` command is self-contained; agents need only their task prompt + role anchor.

---

## Is There Any Dependency to Preserve?

No. The role anchor is the only per-turn injection agents receive, and they actively use it: the `COMPLETION:` line tells agents exactly how to call `agent complete` at the end of their task. This is load-bearing and must not be stripped.

The thread `project_id` column exists (`juggle_db.py:697` — migration 26 added it), so project-scoping of a topic list would be *technically possible* — but there is no topic list to scope. The question is already solved at a lower layer.

---

## Verdict and Recommendation

**No-op.** The separation is already implemented, explicitly commented, and guarded at every hook entry point. The user's framing assumed a gap that was closed before this investigation.

**No code change required or recommended.**

If future work adds a "board summary" feature for agents (not currently planned), the correct enforcement point would be `juggle_context.py:161` — one line controls what all agents see, regardless of how many hooks exist.
