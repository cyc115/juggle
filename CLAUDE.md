# Project Context

This is a javascript project using raw-http.


Required environment variables (no defaults):
- _JUGGLE_TEST_DB (src/juggle_cli.py)
- CLAUDE_PLUGIN_DATA (src/juggle_cli.py)
- JUGGLE_MAX_BACKGROUND_AGENTS (src/juggle_db.py)
- JUGGLE_MAX_THREADS (src/juggle_db.py)

Read .codesight/wiki/index.md for orientation (WHERE things live). Then read actual source files before implementing. Wiki articles are navigation aids, not implementation guides.
Read .codesight/CODESIGHT.md for the complete AI context map including all routes, schema, components, libraries, config, middleware, and dependency graph.

# Design Philosophy

- **Code over prompts.** Logic that can be implemented in code must be implemented in code — not encoded in LLM skills or prompt instructions. Detection, state machines, timeouts, recovery branching → Python CLI subcommands.
- **Lightweight orchestrator.** Reuse existing components (DB tables, CLI patterns, tmux primitives) before introducing new abstractions. When a design decision could blow up complexity, stop and ask or cut to the simplest viable approach.
- **Simple ≠ MVP.** Production quality, minimal new concepts.
- **Reliability.** Juggle's behavior must be predictable and consistent. Prefer explicit state, deterministic code paths, and fail-loud errors over silent failures or ambiguous states.

# Versioning

After every major implementation (new feature, significant behavior change):
1. Bump `version` in `.claude-plugin/plugin.json` (patch = bug/minor, minor = new feature)
2. Commit with `feat:` or `fix:` prefix and version in the message body
3. Mark task done in `/Users/mikechen/Documents/personal/projects/juggle/TODO.md`

# Task Tracking

Before starting any new feature or task, add an open entry to:
`/Users/mikechen/Documents/personal/projects/juggle/TODO.md`

Format: `- [ ] <short description> — <details if needed>`

Mark in-progress with `🔄 [IN PLANNING]` or `🔄 [IN PROGRESS]` as appropriate.
Mark done with `- [x] <description> ✅ YYYY-MM-DD` and move to the Done section.
