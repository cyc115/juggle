# TODO

## In Progress

(none)

## Done

- [x] Pluggable sub-agent harness adapters (`juggle_harness.py`): refactor the Claude-only launch logic in `JuggleTmuxManager.start_agent_in_pane` behind a `HarnessAdapter` so juggle can drive Codex / reasonix / any CLI. Hybrid design — built-in `ClaudeCodeAdapter` (per-role `--settings` overlay) + config-only `TemplateHarnessAdapter` (command/markers/env in `config.json`, no Python). Selectable via `agent.harness` / `agent.harness_by_role` / `agent.harnesses`. Readiness/submission markers and env scrubbing are now per-harness; non-hook harnesses get the role anchor inlined into the task prompt (`decorate_task`). Back-compat: missing `harnesses` block synthesises the built-in claude harness from `claude_launch_command` → zero behaviour change. Docs in `docs/harness-adapters.md`. ✅ 2026-05-31
- [x] Per-agent tool-usage telemetry to right-size the deny block (`juggle agent-tools`): `PreToolUse` logs every agent tool call to a new `agent_tool_events` table (out-of-band hook subprocess → zero agent context tokens; upsert-aggregated by role×tool×mode). New `agent.audit_mode` flag relaxes per-role denies (keeps universal base) and tags agents `JUGGLE_AGENT_AUDIT=1` so true per-role demand becomes observable — stripped tools are otherwise invisible (never offered to the model). `juggle agent-tools` report cross-references usage against each role's configured deny: flags denied-but-used tools (⚠ over-aggressive → ALLOW) and cross-role unused tools (candidates to DENY). ✅ 2026-05-31
- [x] Add otterai to universal agent deny; Google Workspace connectors (Drive/Calendar/Gmail) denied for coder+planner, kept for researcher ✅ 2026-05-31
- [x] Deny GitHub MCP for all agents (orchestrator owns GitHub) ✅ 2026-05-31
- [x] Generate per-role agent `settings.json` overlays (`juggle_agent_settings.py`): write role denials to a file and launch agents with `--settings <path>` instead of a long `--disallowedTools a,b,c,…` flag pasted into tmux (unreliable for large lists). `--settings` layers over the host settings hierarchy (omitted keys keep host values; permission arrays union), so the overlay is additive and portable across dev environments. `settings_overlay_base`/`settings_overlay_by_role` config keys keep per-role divergence (env/model/hooks/sandbox) possible — empty today. ✅ 2026-05-31
- [x] Stop agent sessions from inheriting orchestrator context (token saving): guard `UserPromptSubmit`, `SessionStart`, and `PostToolUse` hooks + `juggle_context._build` on `JUGGLE_IS_AGENT`. Agents now get only their role anchor instead of the full "JUGGLE ACTIVE" dashboard (~2000 tokens/turn), the `/juggle:start` startup tree at boot, and per-read "ORCHESTRATOR VIOLATION" warnings. ✅ 2026-05-31
