# M7 — orchestrator on pi (OUTLINE — do not execute yet)

> **For agentic workers:** NOT executable in this form. After the M6
> retrospective, a planning agent expands this outline into a full task-level
> plan (same format as M1–M6) incorporating M0–M6 learnings. Anything started
> from this outline directly is a defect.

**Goal (S3):** the user's interactive orchestrator session runs in pi instead
of Claude Code; juggle ships as a pi package. Claude Code orchestrator support
is retained during a dual period.

**Known workstreams to expand:**

1. **Wake bridge unification.** Bridge extension watches the juggle DB
   (notification cursor) in-process; agent completions → `pi.sendUserMessage`.
   Retires Monitor daemon, Cron fallback, ScheduleWakeup loop on pi. Autopilot
   flag file dies (DB setting only). Enforcement: extension code.
2. **Commands port.** 31 `commands/*.md` → pi prompt templates +
   `registerCommand` for programmatic ones; `${CLAUDE_PLUGIN_ROOT}` →
   `JUGGLE_REPO_ROOT`; `allowed-tools` → gate extension; verify colon-name
   namespacing, else flatten. `commands/start.md` orchestrator protocol
   re-authored against pi tool names.
3. **ask_user tool.** `registerTool` + `ctx.ui.select` replacing
   AskUserQuestion; wire into the existing open-question lifecycle
   (`juggle_hooks_askuser.py`) via hook-event. Required for Working Rules
   decision-UI parity.
4. **Checkpoint via session file.** PreCompact checkpoint →
   `session_before_compact` + `appendEntry`; drop `checkpoint.json`.
5. **Orchestrator lockdown.** Edit/Write denial + read-warning rules via gate
   extension in the orchestrator session (roles: orchestrator vs agent).
6. **Packaging.** pi package bundling extensions + prompts + skills;
   `juggle_version_bump.py` retargets `package.json`; decide plugin.json fate;
   fix marketplace.json drift while touching it.
7. **Status widget.** `ctx.ui.setWidget` agent panel (replaces statusline
   scraping-adjacent UX); cockpit `f`/`t` semantics revisited.

**Open questions to resolve at planning time (AskUserQuestion the genuine
ones):** billing path for orchestrator model (subscription OAuth "extra usage"
vs API key); whether Claude Code orchestrator support is retired or kept
indefinitely; MCP-dependent commands (`search`, `deep-research`, `capture`)
replacement strategy.

**Exit criteria for 2.0:** one week of daily-driver use on pi orchestrator +
pi workers with zero scraping-layer invocations; then a retirement decision on
the legacy paths.
