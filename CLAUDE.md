# Project Context

Python CLI project (Claude Code plugin). Source in `src/`.

Required environment variables (no defaults):
- _JUGGLE_TEST_DB, CLAUDE_PLUGIN_DATA (juggle_cli.py)
- JUGGLE_MAX_BACKGROUND_AGENTS, JUGGLE_MAX_THREADS (juggle_db.py)

# Design Philosophy

- **Code over prompts.** Logic and behavioral rules go in code or hooks — never prompt-only. Prompts can be forgotten; CLI commands and hooks cannot.
- **Lightweight orchestrator.** Reuse DB tables, CLI patterns, tmux primitives before new abstractions. Cut to simplest viable approach.
- **Simple ≠ MVP.** Production quality, minimal new concepts.
- **Reliability.** Explicit state, deterministic code paths, fail-loud errors.

# Versioning

After every major implementation:
1. Bump `version` in `.claude-plugin/plugin.json` (patch = bug/minor, minor = feature)
2. Commit with `feat:`/`fix:` prefix and version in body
3. Mark done in `/Users/mikechen/Documents/personal/projects/juggle/TODO.md`

# Task Tracking

Track in `/Users/mikechen/Documents/personal/projects/juggle/TODO.md`:
- New: `- [ ] <description>`
- In-progress: prefix with `🔄 [IN PLANNING]` or `🔄 [IN PROGRESS]`
- Done: `- [x] <description> ✅ YYYY-MM-DD` (move to Done section)
