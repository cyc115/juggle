# TODO

## In Progress

(none)

## Done

- [x] Stop agent sessions from inheriting orchestrator context (token saving): guard `UserPromptSubmit`, `SessionStart`, and `PostToolUse` hooks + `juggle_context._build` on `JUGGLE_IS_AGENT`. Agents now get only their role anchor instead of the full "JUGGLE ACTIVE" dashboard (~2000 tokens/turn), the `/juggle:start` startup tree at boot, and per-read "ORCHESTRATOR VIOLATION" warnings. ✅ 2026-05-31
