# M6 — juggle-pi bridge + role-gating extensions

> **For agentic workers:** execute with superpowers:executing-plans. Depends on
> M5 merged into `juggle-2.0`. TypeScript work — keep the TS layer THIN: pure
> I/O shim; every behavior decision stays in Python (Design Philosophy: code
> over prompts, one source of truth).

**Goal:** restore hook-level features for pi workers via two small pi
extensions, and close the C7 restriction gap declared in M5:
1. **bridge** — subscribes to pi events, normalizes payloads, shells to
   `juggle_cli hook-event <name>` (JSON stdin→stdout), applies the response.
2. **gate** — per-role tool restriction (`setActiveTools` + `tool_call` block),
   audit mode relaxation, tool-use telemetry.

## Files Touched

| File | Action |
|---|---|
| `extensions/juggle-pi/bridge.ts` | Create — event subscribe → exec juggle CLI → apply `{inject_message?, block?, reason?, send_user_message?}` |
| `extensions/juggle-pi/gate.ts` | Create — reads `JUGGLE_AGENT_ROLE`/`JUGGLE_AGENT_AUDIT`; deny-by-role from a JSON artifact juggle writes at dispatch; emits `agent_tool_events` rows via CLI |
| `extensions/juggle-pi/package.json` | Create — pi package manifest (extensions only) |
| `src/juggle_cli_commands_misc.py` (or new `juggle_cmd_hook_event.py`) | Create — `juggle hook-event <name>`: normalized schema → existing `juggle_hooks` HANDLERS |
| `src/juggle_hooks.py` + `juggle_hooks_*.py` | Modify — accept normalized payload (adapter, not rewrite); Claude hooks keep working unchanged |
| `src/harnesses/pi.py` | Modify — `supports_hooks:true`; drop `external_restriction`; write per-role deny artifact; pass extension paths via pi settings/`-e` |
| `docs/hook-event-schema.md` | Create — THE contract: fields (`event`, `session_id`, `cwd`, `tool_name`, `tool_input`, `prompt`, `reason`, …) and response shape |
| `tests/test_hook_event_cli.py`, `tests/test_pi_gate_artifact.py` | Create |
| `tests/test_harness_conformance.py` | Modify — new rows: C10 (structured state OR markers), C11 (hook bridge OR inlined anchor), C7 now satisfied by gate artifact for pi |

## Tasks (sequential)

- [ ] **1. Schema first.** Write `docs/hook-event-schema.md`; RED tests for
  `juggle hook-event` covering: UserPromptSubmit-equivalent (inject context),
  PreToolUse-equivalent (block Edit outside /tmp for orchestrator sessions),
  agent-role-anchor injection, AgentStop-equivalent (completion check →
  nudge/ok). Reuse existing handler functions — the CLI is a payload adapter.
- [ ] **2. bridge.ts.** `before_agent_start` → hook-event → inject `{message}`;
  `tool_call` → block/mutate per response; `agent_end` → completion check →
  `pi.sendUserMessage(nudge)` bounded by extension-local counter;
  `session_before_compact` → checkpoint call. Total ≤200 lines; no logic
  beyond mapping. Node integration test with mocked `pi` API object.
- [ ] **3. gate.ts.** Load deny artifact written by the adapter at dispatch
  (`~/.juggle/agent-settings/<agent>-pi.json`); enforce via `setActiveTools`
  at session start + `tool_call` block as backstop; audit mode = log-only.
  ≤150 lines. RED test: denied tool blocked; audit relaxes; telemetry row lands.
- [ ] **4. Adapter wiring.** pi harness declares `supports_hooks:true`; role
  anchor moves from inlined-prompt to bridge injection (C8: exactly once —
  RED test both paths never double-inject).
- [ ] **5. Conformance C10/C11.** Add capability-based rows so ALL harnesses
  (claude, codex, reasonix, pi) pass by declaring which mechanism they use;
  a harness declaring neither fails loudly.
- [ ] **6. CI.** Add pinned-pi + node to the test env for extension unit tests
  (skip-marker ONLY if pi binary absent locally, never in integrate — document
  in `docs/ARCHITECTURE.md` § Integrate test environment).

## Enforcement

Restriction = extension code + artifact (schema), no longer declared-gap.
Bridge contract = versioned schema doc + CLI tests (code). TS thinness =
line caps + review rule stated here. Role anchor once = conformance C8.

## Definition of done

Full pytest green; conformance green all harnesses; node extension tests
green; live smoke: one pi agent dispatched with gating active, denied tool
blocked, completion recorded via bridge (evidence in PR); paste summary lines.
