# Migrating juggle to the pi coding harness — research report

Date: 2026-08-07
Status: research / feasibility (no code changes)
Scope: (a) inventory of Claude Code capabilities juggle depends on, (b) technical
profile of pi's integration surface, (c) capability-by-capability mapping with
redesigns needed, (d) recommended architecture and phased plan.

---

## 1. Executive summary

**juggle should not "migrate off" Claude Code — it should promote pi to a
first-class harness inside the existing `HarnessAdapter` framework, and
optionally later move the orchestrator session itself onto pi.** Three distinct
scopes hide inside "migrate to pi", with wildly different costs:

| Scope | What it means | Cost | Verdict |
|---|---|---|---|
| **S1. pi as a sub-agent harness (one-shot)** | Graph/background agents run `pi -p` instead of `claude` | ~0.5–1 day, config-only (`type: "template"`) | Do now; works like the shipped `codex`/`reasonix` paths, same feature losses (`supports_hooks:false`) |
| **S2. pi as a first-class sub-agent harness** | `PiAdapter` + RPC supervisor + a `juggle-pi` bridge extension restoring hook-level features | ~3–4 weeks | The high-value target. pi's RPC mode + extension API **eliminate juggle's two most fragile subsystems** (TUI screen-scraping, the triple wake bridge) |
| **S3. orchestrator session on pi** | The user's interactive session runs in pi; the whole plugin (hooks/commands/skills/packaging) is ported | +2–4 weeks on top of S2 | Feasible — pi has equivalents for every hook contract juggle uses — but do it last, behind a dual-harness period |

The headline finding: **pi's integration surface is a superset of what juggle
needs from Claude Code hooks** (context injection, tool veto *and mutation*,
message injection into a live session, compaction interception), and its RPC
mode replaces pane-scraping with structured state. The two real costs are
(1) pi extensions are **in-process TypeScript**, not language-agnostic JSON
subprocesses — juggle's Python hook logic needs a thin TS shim that shells back
into `juggle_cli.py`; and (2) **pi has no permission system**, so juggle's
per-role `permissions.deny` overlay must be reimplemented as a tool-gating
extension (which the C7 conformance test would then gate).

---

## 2. Inventory: Claude Code capabilities juggle uses

Grouped by integration surface; file references are current as of `9ccdc79`.

### 2.1 Plugin packaging
- `.claude-plugin/plugin.json` (v1.128.3) + `.claude-plugin/marketplace.json`
  (version drifted: 1.11.0 — latent bug), convention-discovered `hooks/`,
  `commands/`, `skills/`.
- Version-bump automation targets the manifest: `src/juggle_version_bump.py:22`,
  custom merge driver `scripts/git-merge-plugin-version.py`.
- Install-dir guards: `src/juggle_repo_binding.py:164` refuses to bind agents to
  `~/.claude`/plugin-cache paths.

### 2.2 Lifecycle hooks — the deepest surface (~1,600 LOC across `src/juggle_hooks*.py`)
Registered in `hooks/hooks.json`, all dispatching to `src/juggle_hooks.py <Event>`:

| Event | What juggle does with it |
|---|---|
| `UserPromptSubmit` | Injects the thread dashboard + autopilot directive as `additionalContext` every turn (`juggle_hooks_prompt.py`); records the user turn; auto-approves safe permission prompts in agent panes via `tmux capture-pane`/`send-keys`; agent-side: injects the role anchor |
| `SessionStart` | Restores the pre-compaction checkpoint; injects startup context (`juggle_hooks_checkpoint.py`) |
| `PreCompact` | Atomic checkpoint of session id / active thread / in-flight dispatches to `~/.claude/juggle/checkpoint.json` |
| `PreToolUse` (Edit/Write/NotebookEdit/AskUserQuestion) | **Hard-denies** orchestrator file edits (`permissionDecision:"deny"`, stderr JSON, exit 2 — `juggle_hooks_tooluse.py:171`); records `AskUserQuestion` open-question lifecycle (`juggle_hooks_askuser.py`) |
| `PostToolUse` (Read/Glob/Grep/Agent/AskUserQuestion) | Warns on orchestrator-lockdown violations and foreground `Agent` calls via `additionalContext`; closes AskUserQuestion items |
| `Stop` | Class-B transcript scan for machinery-error self-heal (`juggle_hooks_classb.py`); talkback hook (`scripts/talkback-stop-hook`) |
| `AgentStop` (injected per-agent via `--settings` overlay, `juggle_agent_settings.py:69`) | `{"decision":"block","reason":…}` — refuses to let an agent's turn end until it runs `juggle agent complete/fail`; `stop_hook_active` loop guard |

stdin fields consumed: `prompt`, `session_id`, `cwd`, `transcript_path`,
`last_assistant_message`, `stop_hook_active`, `reason`, `tool_name`,
`tool_input`, `tool_use_id`. stdout contracts emitted: `additionalContext`,
`permissionDecision:"deny"` + `systemMessage` + exit 2, `{"decision":"block"}`,
bare `systemMessage`. `UserPromptSubmit` is deliberately fail-open
(`juggle_hooks.py:115`).

### 2.3 Slash commands & skills
- 31 `commands/*.md`: frontmatter `description`/`name`/`allowed-tools`,
  `$ARGUMENTS` (13 files), colon-namespacing (`project:create.md`),
  `${CLAUDE_PLUGIN_ROOT}` in 28 files. **Not used:** `argument-hint`,
  `` !`bash` `` preambles, `@file` refs. All commands are thin wrappers over
  `uv run …/juggle_cli.py` — the logic already lives outside the prompts.
- 6 skills (flat `.md` + `SKILL.md` dirs) with non-standard `triggers:` and
  `schedule:` frontmatter that **juggle itself** consumes, not Claude Code.
  External skill deps hardcoded in prompts: `superpowers:*`, `mike:pre-pr`
  (config `agent.quality_gate_skill`, `juggle_settings.py:133`).

### 2.4 `claude` binary invocation
- Interactive agents: `claude --dangerously-skip-permissions --model <m>
  [--effort <e>] --settings <overlay.json>` (`juggle_settings.py:125`,
  assembled by `juggle_harness.py:172`). **Not used anywhere:** `--resume`,
  `--continue`, `--append-system-prompt`, `--session-id`, `stream-json`,
  `--mcp-config`.
- Headless: `run_claude_p()` SSOT (`src/llm_calls.py:23`) — `claude -p … 
  --output-format json`, parses `result` + `usage.*`; `JUGGLE_INTERNAL_LLM=1`
  self-recursion guard because `-p` re-fires juggle's own hooks
  (`juggle_hooks_prompt.py:111`). One SSOT bypass:
  `schedules/dogfood_research.py:60`.
- Per-role restriction: additive `--settings` overlay with `permissions.deny`
  arrays naming ~30 Claude Code tool identifiers
  (`juggle_agent_overlay_defaults.py`), plus forced `"editorMode":"normal"`
  (vim mode breaks tmux paste).

### 2.5 TUI screen-scraping — the agent state machine
State detection is pane-scraping of the Claude Code TUI. Only 3 marker sets are
adapter-configurable (`readiness_markers`, `submission_markers`,
`active_status_pattern`); the rest are hardcoded with no seam:
- Ready/submitted markers (`juggle_spawn_readiness.py:20-21`), spinner
  timer + **hardcoded list of Claude's whimsical thinking verbs**
  (`juggle_watchdog_paneparse.py:23`), context-usage regex parsing
  `"Sonnet 4.6(164.0k/200.0k)"` (`:19`), Claude-alive markers
  (`juggle_watchdog.py:74`), permission-dialog strings (`:53`), the `│ ❯ … │`
  input-box frame parser (`juggle_paste_submit.py:30`), collapsed-paste and
  queued-message wording.
- Repo comments document ≥5 incidents caused by Claude Code UI copy changes.
  This subsystem is fragile *even without* migrating.

### 2.6 tmux dispatch protocol
`JuggleTmuxManager` (`src/juggle_tmux.py`): pane spawn, `load-buffer` →
`paste-buffer -p -r` (bracketed paste, LF preserved) → `send-keys C-m`,
readiness polling (120×1s), a 2-rung unsubmitted-recovery ladder
(`juggle_paste_submit.py:199`), warm-pane reuse with literal `/clear`
(`juggle_dispatch_acquire.py:53`, already harness-gated), one-shot mode
(`run_task_oneshot`, `juggle_tmux.py:461`) for `interactive:false` harnesses.

### 2.7 Wake/notification bridges (3 proprietary mechanisms)
- **Monitor tool** (primary): `commands/start.md` arms
  `scripts/juggle-agent-monitor` (`src/juggle_monitor_daemon.py`) streaming
  agent-completion lines into the orchestrator conversation.
- **CronCreate/CronList/CronDelete** fallback (`src/juggle_cmd_monitor.py:20`,
  `commands/doctor:enable-legacy-monitor.md`).
- **ScheduleWakeup** self-loop for autopilot (`commands/toggle-autopilot.md`).
Without one of these, the conversational orchestrator never learns an agent
finished (the watchdog covers headless graph work independently).

### 2.8 Session/transcript introspection
- Parses Claude Code transcript JSONL: Class-B error attribution
  (`juggle_hooks_classb.py:56`, schema known by observation) and per-run token
  accounting under `~/.claude/projects/` — **reimplements Claude's project-dir
  naming** `re.sub(r"[/.]","-",cwd)` (`juggle_run_tokens.py:18`).
- Writes `~/.claude.json` folder-trust (`juggle_claude_trust.py:30`,
  env-overridable).
- Data dir `~/.claude/juggle/` (DB, log, checkpoint).

### 2.9 Env vars & model namespace
- `CLAUDE_PLUGIN_ROOT` (hooks.json + 28 command files; deliberately **not**
  used in Python — replaced by `JUGGLE_REPO_ROOT` after the 2026-07-03
  stale-plugin-cache incidents), `CLAUDE_PLUGIN_DATA` (only ever scrubbed),
  `CLAUDE_CODE_SESSION_ID` (session TTL heartbeat, monitor cursor keys).
- `juggle_model_registry.py` validates `claude-*` model ids — already gated on
  `harness_id == "claude"`.

### 2.10 Claude Code tools juggle *tracks or denies* (not calls)
`AskUserQuestion` (full open-question lifecycle), `Agent`/Task (violation
detection only; denied to agents), Bash `run_in_background` (verify-spawn cap,
`juggle_verify_cap.py`), plan-mode/worktree/cron tools (denied in overlays),
MCP tools (`mcp__web-search__*`, `mcp__personal-mcp__*` required by
`search.md`/`deep-research.md`/`capture.md`; per-role deny slugs).

### 2.11 Existing portability assets
- `HarnessAdapter` framework (`src/juggle_harness.py`, `src/harnesses/{claude,codex}.py`,
  `juggle_harness_defaults.py` with a config-only `reasonix`), documented in
  `docs/harness-adapters.md`.
- **C1–C9 conformance suite** (`tests/test_harness_conformance.py`) —
  auto-discovers every harness, no opt-out. The single strongest migration asset.
- Known leaks past the abstraction (8, per audit): global-harness marker
  resolution, hardcoded watchdog/paneparse/paste-submit markers, `/clear`
  gating, claude-only model validation, unconditional `~/.claude.json` trust
  write at spawn (`juggle_tmux.py:596`), force-injected AgentStop overlay.

---

## 3. pi technical profile (as of 2026-08-07)

pi is Mario Zechner's (badlogic) minimal coding agent, now stewarded by
Earendil: npm `@earendil-works/pi-coding-agent` (0.84.1, published 2026-08-07),
MIT, TypeScript monorepo (`pi-mono`: coding agent + `pi-agent-core` + `pi-ai`
multi-provider LLM layer + `pi-tui` + telemetry). Docs: https://pi.dev/docs/latest.
Philosophy: tiny core (7 built-in tools: read/bash/edit/write/grep/find/ls,
<1k-token system prompt); everything else is extensions, skills, and packages.

Key surfaces relevant to juggle:

- **Extensions** (https://pi.dev/docs/latest/extensions): TypeScript modules
  (`~/.pi/agent/extensions/*.ts`, project `.pi/extensions/`, `pi -e`), loaded
  in-process via jiti, hot-reloadable. API: `pi.on(event, handler)`,
  `registerTool/Command/Shortcut/Flag`, **`sendMessage`/`sendUserMessage`**
  (inject messages into the live session), `appendEntry` (persist state in the
  session file), `get/setActiveTools`, `setModel`, `exec`, custom renderers,
  `ctx.ui.{notify,confirm,select,input,setStatus,setWidget,setTitle}`.
- **Event catalog** ≈ hooks, richer: `session_start` (startup/resume/fork
  reasons), **`before_agent_start`** (return `{message, systemPrompt}` — inject
  context or replace the system prompt), **`tool_call`** (block with
  `{block:true, reason}` **and mutate `event.input` in place**), `tool_result`
  (patch results), `agent_start/end/settled`, `turn_start/end`,
  `message_start/update/end`, **`context`** (filter/rewrite the full message
  array before every LLM call — no Claude Code equivalent),
  `session_before_compact`/`compact` (cancellable; custom summaries), `input`,
  `before/after_provider_*`.
- **RPC mode** (https://pi.dev/docs/latest/rpc): `pi --mode rpc` = persistent
  bidirectional JSONL session server over stdio: `prompt`, **`steer`**,
  `follow_up`, `abort`, `new_session`, `fork`, `get_entries` (incremental
  cursor sync), `get_state`, **`get_session_stats`** (token usage), `compact`,
  `set_model`. Also `--mode json` (event stream ≈ `--output-format
  stream-json`) and `pi -p` print mode.
- **Prompt templates** (https://pi.dev/docs/latest/prompt-templates): Markdown +
  YAML frontmatter (`description`, `argument-hint`), `$ARGUMENTS`/`$1`/`${@:N}`,
  in `~/.pi/agent/prompts/` and `.pi/prompts/` — near drop-in for juggle's
  command format (no `allowed-tools` key; gating moves to extensions).
- **Skills** (https://pi.dev/docs/latest/skills): Anthropic Agent Skills
  standard, progressive disclosure, `/skill:<name>`; settings can point
  directly at `~/.claude/skills`. **AGENTS.md or CLAUDE.md** loaded natively
  (cwd + parents + global).
- **Sessions** (https://pi.dev/docs/latest/session-format): documented JSONL
  **trees** (v3) at `~/.pi/agent/sessions/--<cwd-slug>--/`, entries carry
  `id`/`parentId`; `CustomEntry` lets extensions persist non-context state in
  the session file.
- **Providers**: Anthropic API key **and Claude Pro/Max OAuth** (caveat:
  third-party-harness subscription use draws from "extra usage", billed per
  token), OpenAI OAuth, Copilot, OpenRouter, 30+ providers, mid-session
  `/model` switch.
- **Packages** (https://pi.dev/docs/latest/packages): `pi install
  npm:@scope/pkg | git:… | /path`; a package bundles extensions + skills +
  prompts + themes via a `pi` manifest in package.json. This is the plugin
  manifest/marketplace analogue.
- **Sub-agents**: none built-in (deliberate; docs say "use tmux"). Official
  subagent extension examples + community `@tintinweb/pi-subagents`
  (Claude-Code-style Agent tool, `.pi/agents/*.md`, parallel background
  execution, live TUI widget).
- **Gaps**: **no permission system** (no allow/ask/deny; only a per-project
  trust prompt gating `.pi/` resource loading; official stance: sandbox or
  containerize), **no MCP** (Zechner is publicly anti-MCP; community
  `pi-mcp-adapter` exists), no plan mode, no built-in todos/background bash.
- Ecosystem: ~50 official extension examples (subagent spawner, safety gates,
  custom footers, git checkpoints, custom compaction), awesome-pi lists, a
  documented Claude Code→pi migration write-up (danielkoller.me).
- **Unverified**: an official `CLAUDE_PLUGIN_DATA`/`CLAUDE_PROJECT_DIR`
  equivalent (`PI_CODING_AGENT_DIR` appears in community docs only); exact
  billing details of subscription OAuth in third-party harnesses.

---

## 4. Capability → pi mapping

| # | Claude Code capability (juggle usage) | pi equivalent | Redesign needed | Difficulty |
|---|---|---|---|---|
| 1 | `UserPromptSubmit` → `additionalContext` (dashboard, autopilot, role anchor) | `before_agent_start` returning `{message}`; `input` event | Bridge extension calls `juggle_cli hook-event`, returns its stdout as the injected message | Medium |
| 2 | `SessionStart` injection + checkpoint restore | `session_start` (has resume/fork reasons) + `before_agent_start` | Same bridge path | Low |
| 3 | `PreCompact` checkpoint | `session_before_compact` (cancellable) + `compact` | Direct equivalent — *better*: custom summaries possible; checkpoint could move into the session file via `appendEntry` | Low |
| 4 | `PreToolUse` hard-deny (exit 2 / `permissionDecision`) | `tool_call` → `{block:true, reason}` | Direct equivalent — *stronger* (can also rewrite `event.input`) | Low |
| 5 | `PostToolUse` warnings / AskUserQuestion close | `tool_result` (patch) or `sendMessage` | Direct | Low |
| 6 | `AgentStop` `{"decision":"block"}` completion enforcement | No 1:1 "block turn end". Redesign: on `agent_end`/`agent_settled`, if no `agent complete/fail` recorded, `sendUserMessage("run juggle agent complete …")` → drives another turn; extension-local loop guard replaces `stop_hook_active` | Behavior-equivalent nudge loop instead of a block; must verify no infinite-loop edge | Medium |
| 7 | Per-role `--settings` `permissions.deny` overlay | **None** (no permission system) | New gating extension: read `JUGGLE_AGENT_ROLE`, enforce via `setActiveTools` + `tool_call` block; audit mode relaxes. C7 conformance retargets to this artifact | Medium–High (must re-prove the audit/deny guarantees) |
| 8 | `--dangerously-skip-permissions` | Not needed — pi runs unrestricted by default | Drop flag; rely on gating extension + (recommended) container/sandbox for agents | Low |
| 9 | `claude -p --output-format json` (`run_claude_p`) | `pi -p` / `pi --mode json`; usage from stats events | Swap inside the `llm_calls.py` SSOT; fix the `dogfood_research.py:60` bypass first; keep `JUGGLE_INTERNAL_LLM`-style guard only if the bridge extension is active in `-p` mode (verify) | Low |
| 10 | TUI screen-scraping state machine (~15 marker sets) | **Eliminated**: RPC `get_state`, `get_session_stats`, structured `agent_*`/`tool_execution_*` events; context % from stats, not regex | Rewrite watchdog classification against RPC state; delete paneparse regexes for pi agents | High (but removes juggle's most incident-prone code) |
| 11 | tmux bracketed-paste + `send-keys C-m` dispatch; unsubmitted-recovery ladder | RPC `prompt` (and `steer` for mid-task nudges — replaces the Escape+continue stall ladder) | New `juggle_pi_rpc.py` supervisor owning child `pi --mode rpc` processes; tmux panes become optional *viewers*, not the control channel | High |
| 12 | Warm reuse `/clear` | RPC `new_session` | Adapter capability (`context_reset`), replacing the `if harness == "claude"` gate | Low |
| 13 | Wake bridge: Monitor + Cron fallback + ScheduleWakeup | **Single mechanism**: bridge extension in the *orchestrator's* pi session watches the juggle DB/spool (extensions can run timers/watchers in-process) and `sendUserMessage`s completion lines | Replaces three proprietary mechanisms with one; only available once the orchestrator itself runs on pi (S3). Until then, S1/S2 keep the existing Claude-side bridges | Medium (S3 only) |
| 14 | `AskUserQuestion` lifecycle tracking | No such tool; `ctx.ui.confirm/select` are extension-side | Register a juggle `ask_user` tool via `registerTool` (backed by `ctx.ui.select`), track via `tool_call`/`tool_result` as today | Medium |
| 15 | Transcript parsing (`~/.claude/projects`, undocumented schema) | Documented session-format v3 JSONL + RPC `get_session_stats` | Rewrite `juggle_run_tokens.py` / `juggle_hooks_classb.py` parsers per-harness; net *improvement* (documented format, usage API) | Medium |
| 16 | `~/.claude.json` folder-trust write | pi per-project trust (`defaultProjectTrust` setting; `project_trust` event) | Set `defaultProjectTrust` in agent settings or accept-trust via the bridge; move the trust step behind the adapter (fixes leak #7) | Low |
| 17 | 31 slash commands (`allowed-tools`, `$ARGUMENTS`, colon-namespacing, `${CLAUDE_PLUGIN_ROOT}`) | Prompt templates (`$ARGUMENTS` compatible) + `registerCommand` for programmatic ones | Re-author frontmatter (drop `allowed-tools` → gating extension); replace `${CLAUDE_PLUGIN_ROOT}` with `JUGGLE_REPO_ROOT` exported by the bridge; colon names → check pi's namespacing, else flatten | Medium (mechanical; logic already lives in `juggle_cli.py`) |
| 18 | Skills | Native Anthropic-skills support; can even read `~/.claude/skills` | Near drop-in; juggle's custom `triggers:`/`schedule:` keys are juggle-consumed and carry over untouched | Trivial |
| 19 | CLAUDE.md / AGENTS.md context | Both loaded natively | None | Trivial |
| 20 | Plugin manifest + marketplace + version-bump | pi package (`pi` manifest in package.json, `pi install git:…`) | Add package.json manifest; retarget `juggle_version_bump.py` + merge driver to it; fix the stale `marketplace.json` drift while at it | Low |
| 21 | Claude model registry (`claude-*`) | pi-ai `provider/model` ids | Already harness-gated; add pi namespace validation in the adapter | Low |
| 22 | MCP tools in commands (`mcp__web-search__*`, `mcp__personal-mcp__*`) | No MCP | Replace with juggle CLI equivalents (offline search + OpenRouter paths already exist) or `pi-mcp-adapter`; per-role MCP deny slugs become moot | Medium |
| 23 | `editorMode:"normal"` forced (vim breaks paste) | Moot under RPC (no editor in the control path) | Drop for pi | Trivial |
| 24 | Statusline / cockpit pane focus | `ctx.ui.setWidget/setStatus` (live widget, as pi-subagents does); pi TUI themes | Optional: juggle status widget in the orchestrator session (S3) | Low |

**Python↔TypeScript boundary (cross-cutting):** every "bridge extension" row
assumes one thin TS extension (~300–500 LOC) whose only job is I/O: subscribe
to pi events, normalize the payload to a juggle-defined hook-event JSON schema,
`pi.exec` → `uv run juggle_cli.py hook-event <name>` (stdin JSON in, stdout
JSON out), and apply the response (inject message / block tool / send message).
This keeps **all behavior in Python** — consistent with "code over prompts" and
with the existing `juggle_hooks.py` dispatch table, which needs only a
harness-neutral payload adapter, not a rewrite. The normalized schema should be
designed once and become the contract the C-suite tests.

---

## 5. Recommended architecture

```
┌─ Orchestrator session (Claude Code today; pi in S3) ─────────────┐
│  hooks / bridge-extension → juggle_cli hook-event (Python SSOT)  │
└──────────────────────────────────────────────────────────────────┘
             │ dispatch                          ▲ completions
             ▼                                   │
┌─ juggle core (Python, unchanged) ────────────────────────────────┐
│  DB · watchdog · graph · integrate · HarnessAdapter registry     │
│    ├─ ClaudeCodeAdapter (tmux paste + markers)   [existing]      │
│    ├─ CodexAdapter (one-shot)                    [existing]      │
│    └─ PiAdapter → juggle_pi_rpc.py supervisor    [new, S2]       │
│         child: pi --mode rpc  (JSONL stdio; state via get_state, │
│         steer for nudges, get_session_stats for tokens)          │
│         + juggle-pi bridge extension (TS shim → juggle_cli)      │
│         + role-gating extension (setActiveTools / tool_call)     │
└──────────────────────────────────────────────────────────────────┘
```

Design decisions:
1. **Keep the adapter framework; don't fork the codebase.** pi becomes the
   best-supported harness, Claude Code remains fully supported. The C1–C9
   conformance suite gates both; add new contract rows (C10: structured-state
   channel or markers; C11: hook-event bridge or inlined anchor) as capabilities
   rather than assumptions.
2. **RPC supervisor replaces scraping, not tmux.** tmux panes stay for the
   cockpit's human-visibility features (`f` focus, `t` tail) — a pi agent can
   still render its TUI in a pane — but *state and control* flow over RPC
   stdio, never `capture-pane`. This deletes the incident-prone marker layer
   for pi agents and shrinks it to legacy-Claude-only.
3. **One normalized hook-event schema** (`juggle_cli hook-event <name>` reading
   JSON on stdin) so Claude hooks and the pi bridge are two front-ends to the
   same Python handlers. The AgentStop "block" becomes a "nudge loop"
   (`sendUserMessage` until completion recorded) under pi — same guarantee,
   different mechanism; pin it with a conformance/regression test.
4. **Security model shifts from deny-lists to gate-extension + sandbox.** pi
   has no permission layer; the role-gating extension restores the token-saving
   deny guarantee (C7), and containerized agents (pi's own recommendation)
   restore the blast-radius guarantee that `--dangerously-skip-permissions`
   never provided anyway.

---

## 6. Phased plan

| Phase | Deliverable | Effort | Risk |
|---|---|---|---|
| **0. Config-only pi harness** | `pi` entry in `HARNESS_DEFAULTS` (`type:"template"`, `command:"pi -p"` or `--mode json`, `interactive:false`, `supports_hooks:false`, sentinel markers). Conformance suite green. Headless graph agents runnable on pi today | 0.5–1 d | Low — proven path (codex/reasonix) |
| **1. PiAdapter + RPC supervisor** | `src/harnesses/pi.py` + `juggle_pi_rpc.py`; watchdog consumes RPC state for pi agents (new seam beside paneparse); token accounting via `get_session_stats`; `steer`-based stall nudges | 1.5–2 w | Medium — new process-supervision code; pi API churn |
| **2. juggle-pi bridge + gating extensions** | TS shim (`hook-event` bridge), role-gating extension, `ask_user` tool, role-anchor injection via `before_agent_start`, completion nudge loop on `agent_end`; `hook-event` normalized schema in Python; conformance rows C10/C11 | 1.5–2 w | Medium — event-semantics parity needs empirical verification |
| **3. Orchestrator on pi (opt-in)** | pi package (`pi install git:…`) bundling extensions + prompts (ported commands) + skills; wake bridge via in-extension DB watcher + `sendUserMessage` (retires Monitor/Cron/ScheduleWakeup on pi); status widget; version-bump retarget | 2–4 w | Higher — UX changes (AskUserQuestion→`ask_user`, no plan mode), billing model if using Claude subscription OAuth |

Total to full S3: roughly 5–8 weeks of focused work, with S1 usable in a day
and S2 (the highest value-per-effort) in ~3–4 weeks. Run dual-harness
throughout; nothing forces a cut-over.

---

## 7. Risks & open questions

1. **pi velocity/stability.** 0.8x, multiple releases per week, stewardship
   recently moved (badlogic → Earendil; npm scope changed). Extension API is
   documented but not versioned-stable; pin the pi version in the adapter and
   test against it in CI.
2. **Event-semantics parity must be verified by experiment**, not docs:
   (a) does `before_agent_start` fire on resume/compaction turns; (b) can the
   `agent_end` nudge loop deadlock or spin (needs the `stop_hook_active`
   equivalent guard); (c) do extensions load in `-p`/`--mode json` runs (the
   `JUGGLE_INTERNAL_LLM` recursion guard question); (d) prompt-template
   namespacing for `project:create`-style names.
3. **No permission system**: until the gating extension exists, a pi agent is
   fully unrestricted — worse than today's deny overlay. Phase 0/1 must either
   accept `external_restriction`-style opt-out explicitly (as reasonix does) or
   run agents sandboxed. The audit-mode telemetry guarantee (C3/C7) needs a new
   enforcement point.
4. **Billing**: Claude Pro/Max OAuth through pi draws from "extra usage"
   (per-token) per pi's docs — running many background agents this way could
   cost materially more than first-party Claude Code. Mitigation: API key /
   OpenRouter for agents (juggle already prefers OpenRouter for internal LLM
   calls).
5. **Node/TS toolchain** becomes a runtime dependency of a currently pure
   Python+uv project (bridge extension, pi itself).
6. **MCP-dependent commands** (`search`, `deep-research`, `capture`) need CLI
   replacements on pi; `pi-mcp-adapter` is community-maintained, treat as
   fallback not foundation.
7. **Lost features with no pi analogue**: plan-mode denial becomes moot (no
   plan mode), `Agent`-tool misuse detection becomes moot (no Task tool unless
   pi-subagents is installed — if it is, gate it the same way), Bash
   `run_in_background` verify-cap needs a different detection point (pi has no
   background bash; long test runs will occupy the agent's turn — arguably
   simpler).
8. **Unverified externally**: official pi env-var equivalents of
   `CLAUDE_PLUGIN_DATA`/`CLAUDE_PROJECT_DIR`; awesome-pi ecosystem counts;
   exact `earendil-works/pi` repo-transfer status.

---

## 8. Decision log (2026-08-08, state-management redesign)

Devil's-advocate pass run before implementation; decisions taken with the user:

1. **Sequencing — state simplification lands BEFORE pi** (picked without asking;
   clearly dominates). The Layer-1/2/4 collapse is harness-agnostic: 6 node
   states (open, running, integrating, done, failed(kind), archived), thread
   status as a projection (no second vocabulary), run ledger append-only (no
   mutable status). Regression pins for migrations 51/54/61 are rewritten to the
   new seams, never weakened.
2. **Worker model — persistent RPC supervisor** (user decision). Mid-task
   `steer` and warm reuse are wanted. Consequences accepted as REQUIRED design
   elements: (a) a FIFO/socket relay between supervisor and `pi --mode rpc`
   children so workers survive a supervisor crash (pi RPC is stdio-bound — no
   native reattach); (b) a dbops-layer single-writer assert for runtime
   transitions; (c) supervisor crash-recovery via session-file replay
   (`get_entries` cursor) + process-table reconcile, built and pinned first.
3. **Proof states — integration record** (user decision). Node stays
   `integrating` until done; proof steps (tests_green → submitted → landed(sha)
   → g1_pass) live as an append-only record in ONE pipeline module; `done` is
   reachable only when the record satisfies its proof (merged_sha, or
   verify_cmd attestation for non-merge topics). Async-land support is kept as
   a record step, not a node state.

Open risks carried forward: pi 0.8x event-contract churn (pin the version; CI
gains node + pinned pi); `agent_end` abort/crash semantics need an empirical
spike; an orchestrator on pi needs a registered `ask_user` tool for decision-UI
parity (Working Rules + AskUserQuestion lifecycle depend on it).

## 9. Sources

- Codebase audit (this repo, `9ccdc79`): `hooks/hooks.json`,
  `src/juggle_hooks*.py`, `src/juggle_harness.py`, `src/harnesses/`,
  `src/juggle_tmux.py`, `src/juggle_watchdog*.py`, `src/juggle_paste_submit.py`,
  `src/juggle_spawn_readiness.py`, `src/juggle_agent_settings.py`,
  `src/juggle_agent_overlay_defaults.py`, `src/llm_calls.py`,
  `src/juggle_run_tokens.py`, `src/juggle_claude_trust.py`,
  `docs/harness-adapters.md`, `docs/ARCHITECTURE.md`,
  `tests/test_harness_conformance.py`, `commands/`, `skills/`.
- pi documentation: https://pi.dev/docs/latest — extensions, rpc, json,
  prompt-templates, skills, sessions, session-format, packages, providers,
  compaction, settings, security, tmux pages.
- https://github.com/badlogic/pi-mono (README, `packages/coding-agent/docs/`,
  `packages/coding-agent/examples/`).
- npm: `@earendil-works/pi-coding-agent` (0.84.1, 2026-08-07);
  legacy `@mariozechner/pi-coding-agent`.
- Community: https://github.com/tintinweb/pi-subagents,
  https://github.com/BubblePtr/awesome-pi, https://awesome-pi.site,
  https://www.danielkoller.me/en/blog/why-pi-is-my-new-coding-agent-of-choice,
  https://newsletter.pragmaticengineer.com/p/building-pi-and-what-makes-self-modifying.
