# TODO

## In Progress

(none)

## Done

- [x] Generate per-role agent `settings.json` overlays (`juggle_agent_settings.py`): write role denials to a file and launch agents with `--settings <path>` instead of a long `--disallowedTools a,b,c,…` flag pasted into tmux (unreliable for large lists). `--settings` layers over the host settings hierarchy (omitted keys keep host values; permission arrays union), so the overlay is additive and portable across dev environments. `settings_overlay_base`/`settings_overlay_by_role` config keys keep per-role divergence (env/model/hooks/sandbox) possible — empty today. ✅ 2026-05-31
- [x] Stop agent sessions from inheriting orchestrator context (token saving): guard `UserPromptSubmit`, `SessionStart`, and `PostToolUse` hooks + `juggle_context._build` on `JUGGLE_IS_AGENT`. Agents now get only their role anchor instead of the full "JUGGLE ACTIVE" dashboard (~2000 tokens/turn), the `/juggle:start` startup tree at boot, and per-read "ORCHESTRATOR VIOLATION" warnings. ✅ 2026-05-31
